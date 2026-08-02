"""Project dmcheck's native result into ``table.evaluation/1.0``.

The projection is deterministic and always self-attested.  A separate
protected host may later bind the same native result to host-owned context;
dmcheck cannot grant that authority to itself.
"""

import hashlib
import json

from ._version import __version__
from .core import public_charter_digest
from .validation import ValidationIssue


TABLE_EVALUATION_SCHEMA_VERSION = "table.evaluation/1.0"


def _canonical_digest(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _diagnostic(value):
    if isinstance(value, ValidationIssue):
        value = value.to_dict()
    code = value.get("code", "dmcheck.diagnostic") if isinstance(value, dict) else "dmcheck.diagnostic"
    message = value.get("message", str(value)) if isinstance(value, dict) else str(value)
    result = {"code": str(code)[:256] or "dmcheck.diagnostic",
              "message": str(message) or "dmcheck diagnostic"}
    pointer = value.get("pointer") if isinstance(value, dict) else None
    if isinstance(pointer, str):
        result["pointer"] = pointer
    return result


def _values(value):
    if isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _values(item)
    elif value is not None and str(value):
        yield str(value)


def _evidence_refs(finding):
    evidence = finding.get("evidence") if isinstance(finding, dict) else None
    refs = []
    if isinstance(evidence, dict):
        for key in ("id", "event_id", "source_id", "correlation_id",
                    "obligation_id", "roll_id", "event_ids", "source_ids",
                    "correlation_ids", "obligation_ids", "roll_ids"):
            for value in _values(evidence.get(key)):
                if len(value) <= 256 and value not in refs:
                    refs.append(value)
    return refs


def _finding(value, policy_version, policy_digest):
    refs = _evidence_refs(value)
    if not refs:
        raise ValueError("TableEvent finding lacks an exact evidence reference")
    severity = "advisory" if value.get("severity") == "advisory" else "finding"
    return {
        "finding_id": str(value["finding_id"])[:256],
        "code": str(value.get("rule") or "dmcheck.finding")[:256],
        "severity": severity,
        "summary": str(value.get("summary") or value.get("detail") or
                       "dmcheck finding"),
        "evidence_refs": refs,
        "effective_policy": dict(value.get("effective_policy") or {}),
        "policy_version": policy_version,
        "policy_digest": policy_digest,
    }


def _rule_coverage(result):
    eligible = set(result.eligible_rules)
    enabled = set((result.charter or {}).get("rules_enabled") or [])
    evaluators = []
    for rule in sorted(set("R%d" % number for number in range(1, 9))):
        if rule in eligible:
            evaluators.append({"id": rule, "required": True,
                               "status": "evaluated", "eligible": 1,
                               "evaluated": 1, "skipped": 0,
                               "skip_reasons": []})
        elif rule not in enabled:
            evaluators.append({
                "id": rule, "required": False, "status": "disabled",
                "eligible": 0, "evaluated": 0, "skipped": 0,
                "skip_reasons": [{"code": "rule.disabled_by_policy",
                                  "message": "%s is disabled by the charter" % rule}],
            })
        else:
            # In a schema-valid, gap-free TableEvent stream, absence of a roll,
            # turn, engine event, player question, or hidden-term policy makes
            # the corresponding conduct rule not applicable; it is not an
            # unreported coverage gap. Adapter blockers return before here.
            evaluators.append({"id": rule, "required": True,
                               "status": "not_applicable", "eligible": 0,
                               "evaluated": 0, "skipped": 0,
                               "skip_reasons": []})
    return evaluators


def project_table_evaluation(result, projection):
    """Return one schema-shaped self-attested portfolio evaluation."""
    if not projection.session_id or not projection.input_digest:
        raise ValueError("a validated TableEvent projection is required")
    charter = result.charter or {}
    policy_digest = (public_charter_digest(charter) if charter
                     else _canonical_digest({"policy": "unavailable"}))
    policy_version = "dmcheck-charter/%s/%s" % (
        charter.get("schema_version", "unknown"),
        charter.get("charter_version", "unknown"))
    native_errors = [_diagnostic(item) for item in result.errors]

    if projection.blockers:
        compatible = projection.compatible_count
        evaluators = [{
            "id": "dmcheck.adapter", "required": True, "status": "error",
            "eligible": compatible, "evaluated": 0, "skipped": compatible,
            "skip_reasons": [_diagnostic(item) for item in projection.blockers],
        }]
    else:
        evaluators = _rule_coverage(result)

    projected_findings = []
    advisories = []
    projection_errors = []
    for item in result.findings:
        try:
            projected = _finding(item, policy_version, policy_digest)
        except (KeyError, TypeError, ValueError):
            projection_errors.append({
                "code": "table_evaluation.finding_evidence_missing",
                "message": "a native finding lacked an exact TableEvent evidence reference",
            })
            continue
        (advisories if projected["severity"] == "advisory"
         else projected_findings).append(projected)

    evaluator_skipped = sum(item["skipped"] for item in evaluators)
    complete = (result.status in {"clean", "findings"}
                and evaluator_skipped == 0 and not projection_errors)
    eligible = projection.compatible_count
    evaluated = eligible if complete else 0
    skipped = eligible - evaluated
    native_errors.extend(projection_errors)

    if not complete:
        codes = {item["code"] for item in native_errors}
        if any(code in {"table_event.unsupported_schema",
                        "table_event.unsupported_type"} for code in codes):
            status = "unsupported"
        elif ("evaluation.failed" in codes
              or "table_evaluation.finding_evidence_missing" in codes):
            status = "internal_error"
        elif result.status == "invalid":
            status = "invalid"
        else:
            status = "incomplete"
    elif projected_findings:
        status = "findings"
    elif advisories:
        status = "checked_with_advisories"
    else:
        status = "checked_clean"

    errors = native_errors
    if not complete and not errors:
        errors = [{"code": "dmcheck.incomplete",
                   "message": "dmcheck could not complete the TableEvent evaluation"}]
    evaluation_material = {
        "tool": "dmcheck", "version": __version__,
        "session_id": projection.session_id,
        "input_digest": projection.input_digest,
        "native_status": result.status,
        "finding_ids": [item["finding_id"] for item in result.findings],
        "error_codes": [item["code"] for item in errors],
    }
    evaluation_id = "dmcheck-" + _canonical_digest(evaluation_material).split(":", 1)[1][:40]
    return {
        "schema_version": TABLE_EVALUATION_SCHEMA_VERSION,
        "evaluation_id": evaluation_id,
        "tool": {"name": "dmcheck", "version": __version__},
        "subject": {"kind": projection.subject_kind, "id": projection.session_id,
                    "session_id": (projection.session_id
                                   if projection.subject_kind == "session" else None),
                    "entity_refs": []},
        "status": status,
        "exit_code": 0 if status == "checked_clean" else (1 if status in {
            "checked_with_advisories", "findings"} else 2),
        "authority_status": "self_attested",
        "coverage": {
            "complete": complete, "evidence_required": True,
            "input": projection.event_count,
            "compatible": projection.compatible_count,
            "eligible": eligible, "evaluated": evaluated,
            "skipped": skipped, "evaluators": evaluators,
        },
        "cursor": {
            "checked_through_event_id": projection.checked_through_event_id,
            "gap_state": ("none" if complete else
                          ("detected" if any(item["code"] == "table_event.transport_gap"
                                             for item in errors) else "unknown")),
            "input_digest": projection.input_digest,
        },
        "context": {"roster_digest": None, "policy_digest": policy_digest,
                    "config_digest": None, "source_set_digest": None,
                    "session_descriptor_digest": None},
        "findings": projected_findings,
        "advisories": advisories,
        "warnings": [],
        "errors": errors,
    }
