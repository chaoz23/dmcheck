"""Strict, dependency-free ``table.event/1.0`` ingestion for dmcheck.

The adapter projects only evidence dmcheck understands.  Contract gaps and
future contract variants are typed incomplete outcomes, never silent drops or
false clean verdicts.
"""

from dataclasses import dataclass, field
import json
import math
import re

from .core import EvaluationResult, evaluate, invalid_result
from .validation import (InputValidationError, issue, load_charter,
                         normalize_timestamp, parse_json_value)


TABLE_EVENT_SCHEMA_VERSION = "table.event/1.0"
KNOWN_EVENT_TYPES = frozenset({
    "session.opened", "session.closed", "message.observed",
    "delivery.received", "transport.gap", "turn.started",
    "action.declared", "roll.observed", "narration.obligation",
    "narration.observed", "evaluation.completed",
})

_SOURCE_KINDS = {"discord", "tablekit", "engine", "dmcheck",
                 "charactercheck", "srdcheck", "host", "fixture", "other"}
_ROLES = {"gm", "player", "agent", "system", "observer", "unknown"}
_VISIBILITY = {"private", "table", "public", "system"}
_SENSITIVITY = {"normal", "personal", "secret"}
_PROVENANCE = {"observed", "derived", "inferred", "reported", "decided"}
_OBLIGATION_KINDS = {"answer", "consume_roll", "narrate_event", "cue"}
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass
class TableEventProjection:
    transcript: list = field(default_factory=list)
    ledger: list = field(default_factory=list)
    event_count: int = 0
    compatible_count: int = 0
    skipped_event_types: list = field(default_factory=list)
    blockers: list = field(default_factory=list)
    campaign_id: str = None
    session_id: str = None


def _pointer_part(value):
    return str(value).replace("~", "~0").replace("/", "~1")


def _nonempty(value):
    return isinstance(value, str) and bool(value) and len(value) <= 256


def _string_id(value, pointer, problems, nullable=False):
    if nullable and value is None:
        return
    if not _nonempty(value):
        problems.append(issue("table_event.id", pointer,
                              "value must be a nonempty string of at most 256 characters"))


def _closed_object(value, pointer, required, allowed, problems):
    if not isinstance(value, dict):
        problems.append(issue("table_event.object", pointer,
                              "value must be an object"))
        return False
    for key in required:
        if key not in value:
            problems.append(issue("table_event.required", pointer + "/" + key,
                                  "%s is required" % key))
    for key in value:
        if key not in allowed:
            problems.append(issue("table_event.unknown_field",
                                  pointer + "/" + _pointer_part(key),
                                  "unknown field %r" % key))
    return True


def _string_array(value, pointer, problems, nonempty=False):
    if (not isinstance(value, list) or (nonempty and not value)
            or any(not _nonempty(item) for item in value)):
        problems.append(issue("table_event.string_array", pointer,
                              "value must be%s an array of nonempty strings"
                              % (" a nonempty" if nonempty else "")))
        return
    if len(set(value)) != len(value):
        problems.append(issue("table_event.duplicate", pointer,
                              "array values must be unique"))


def _enum(value, choices, pointer, problems):
    if value not in choices:
        problems.append(issue("table_event.enum", pointer,
                              "unsupported value %r" % value))


def _digest(value, pointer, problems, nullable=True):
    if nullable and value is None:
        return
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        problems.append(issue("table_event.digest", pointer,
                              "digest must be sha256 followed by 64 lowercase hex characters"))


def _validate_payload(event_type, payload, pointer, problems):
    shapes = {
        "session.opened": ({"descriptor_digest", "authority_status"},
                           {"descriptor_digest", "authority_status"}),
        "session.closed": ({"reason"}, {"reason"}),
        "message.observed": ({"content", "content_redacted"},
                             {"content", "content_redacted"}),
        "delivery.received": ({"operation_id", "message_id", "status"},
                              {"operation_id", "message_id", "status"}),
        "transport.gap": ({"expected_sequence", "observed_sequence", "recoverable"},
                          {"expected_sequence", "observed_sequence", "recoverable"}),
        "turn.started": ({"actor_id"}, {"actor_id"}),
        "action.declared": ({"action_kind", "legal_timing"},
                            {"action_kind", "legal_timing"}),
        "roll.observed": ({"total", "roll_kind"},
                          {"total", "natural", "roll_kind"}),
        "narration.obligation": ({"obligation_id", "kind"},
                                 {"obligation_id", "kind"}),
        "narration.observed": ({"content", "resolves_obligation_ids"},
                               {"content", "resolves_obligation_ids"}),
        "evaluation.completed": ({"evaluation_id", "evaluation_schema", "status",
                                  "authority_status"},
                                 {"evaluation_id", "evaluation_schema", "status",
                                  "authority_status"}),
    }
    required, allowed = shapes[event_type]
    if not _closed_object(payload, pointer, required, allowed, problems):
        return
    if event_type in {"message.observed", "narration.observed"}:
        if not isinstance(payload.get("content"), str):
            problems.append(issue("table_event.content", pointer + "/content",
                                  "content must be a string"))
    if event_type == "message.observed" and not isinstance(payload.get("content_redacted"), bool):
        problems.append(issue("table_event.boolean", pointer + "/content_redacted",
                              "content_redacted must be a boolean"))
    if event_type == "narration.observed":
        _string_array(payload.get("resolves_obligation_ids"),
                      pointer + "/resolves_obligation_ids", problems)
    if event_type == "turn.started":
        _string_id(payload.get("actor_id"), pointer + "/actor_id", problems)
    if event_type == "action.declared":
        _string_id(payload.get("action_kind"), pointer + "/action_kind", problems)
        _enum(payload.get("legal_timing"), {"normal_turn", "reaction", "ready_trigger",
                                            "legendary", "lair", "other", "unknown"},
              pointer + "/legal_timing", problems)
    if event_type == "roll.observed":
        total = payload.get("total")
        if (not isinstance(total, (int, float)) or isinstance(total, bool)
                or not math.isfinite(float(total))):
            problems.append(issue("table_event.number", pointer + "/total",
                                  "total must be a finite number"))
        _string_id(payload.get("roll_kind"), pointer + "/roll_kind", problems)
        natural = payload.get("natural")
        if natural is not None and (not isinstance(natural, int)
                                    or isinstance(natural, bool) or natural < 1):
            problems.append(issue("table_event.integer", pointer + "/natural",
                                  "natural must be null or an integer of at least 1"))
    if event_type == "narration.obligation":
        _string_id(payload.get("obligation_id"), pointer + "/obligation_id", problems)
        _enum(payload.get("kind"), _OBLIGATION_KINDS, pointer + "/kind", problems)
    if event_type == "transport.gap":
        for key in ("expected_sequence", "observed_sequence"):
            value = payload.get(key)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                problems.append(issue("table_event.integer", pointer + "/" + key,
                                      "%s must be a nonnegative integer" % key))
        if not isinstance(payload.get("recoverable"), bool):
            problems.append(issue("table_event.boolean", pointer + "/recoverable",
                                  "recoverable must be a boolean"))
    if event_type == "session.opened":
        _digest(payload.get("descriptor_digest"), pointer + "/descriptor_digest", problems)
        _enum(payload.get("authority_status"), {"self_attested", "host_attested"},
              pointer + "/authority_status", problems)
    if event_type == "session.closed":
        _enum(payload.get("reason"), {"completed", "cancelled", "abandoned", "error"},
              pointer + "/reason", problems)
    if event_type == "delivery.received":
        _string_id(payload.get("operation_id"), pointer + "/operation_id", problems)
        _string_id(payload.get("message_id"), pointer + "/message_id", problems)
        _enum(payload.get("status"), {"received", "partial", "failed"},
              pointer + "/status", problems)
    if event_type == "evaluation.completed":
        _string_id(payload.get("evaluation_id"), pointer + "/evaluation_id", problems)
        if payload.get("evaluation_schema") != "table.evaluation/1.0":
            problems.append(issue("table_event.evaluation_schema",
                                  pointer + "/evaluation_schema",
                                  "evaluation_schema must be table.evaluation/1.0"))
        _enum(payload.get("status"), {"checked_clean", "checked_with_advisories",
                                      "findings", "incomplete", "unsupported",
                                      "invalid", "internal_error"},
              pointer + "/status", problems)
        _enum(payload.get("authority_status"), {"self_attested", "host_attested"},
              pointer + "/authority_status", problems)


def _validate_event(event, index, problems, blockers):
    pointer = "/events/%d" % index
    fields = {"schema_version", "event_id", "campaign_id", "session_id",
              "session_sequence", "source", "occurred_at", "recorded_at",
              "principal", "event_type", "payload", "correlation_ids",
              "causation_id", "audience", "visibility", "sensitivity",
              "provenance", "integrity"}
    if not _closed_object(event, pointer, fields, fields, problems):
        return
    version = event.get("schema_version")
    if version != TABLE_EVENT_SCHEMA_VERSION:
        blockers.append(issue("table_event.unsupported_schema",
                              pointer + "/schema_version",
                              "dmcheck supports table.event/1.0, not %r" % version))
        return
    event_type = event.get("event_type")
    known_event_type = event_type in KNOWN_EVENT_TYPES
    if not known_event_type:
        blockers.append(issue("table_event.unsupported_type", pointer + "/event_type",
                              "dmcheck does not yet support event type %r" % event_type))
    for key in ("event_id", "campaign_id", "session_id"):
        _string_id(event.get(key), pointer + "/" + key, problems)
    sequence = event.get("session_sequence")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
        problems.append(issue("table_event.sequence", pointer + "/session_sequence",
                              "session_sequence must be a positive integer"))
    for key in ("occurred_at", "recorded_at"):
        if not isinstance(event.get(key), str):
            problems.append(issue("table_event.timestamp_type", pointer + "/" + key,
                                  "TableEvent timestamps must be ISO-8601 strings"))
            continue
        try:
            normalize_timestamp(event.get(key), pointer + "/" + key)
        except InputValidationError as exc:
            problems.extend(exc.issues)
    _string_array(event.get("correlation_ids"), pointer + "/correlation_ids", problems)
    _string_id(event.get("causation_id"), pointer + "/causation_id", problems,
               nullable=True)
    _string_array(event.get("audience"), pointer + "/audience", problems, nonempty=True)
    _enum(event.get("visibility"), _VISIBILITY, pointer + "/visibility", problems)
    _enum(event.get("sensitivity"), _SENSITIVITY, pointer + "/sensitivity", problems)
    _enum(event.get("provenance"), _PROVENANCE, pointer + "/provenance", problems)

    source = event.get("source")
    source_fields = {"kind", "instance", "native_id", "sequence", "attestation"}
    if _closed_object(source, pointer + "/source", source_fields, source_fields, problems):
        _enum(source.get("kind"), _SOURCE_KINDS, pointer + "/source/kind", problems)
        _string_id(source.get("instance"), pointer + "/source/instance", problems)
        _string_id(source.get("native_id"), pointer + "/source/native_id", problems,
                   nullable=True)
        native_sequence = source.get("sequence")
        if native_sequence is not None and (not isinstance(native_sequence, int)
                                            or isinstance(native_sequence, bool)
                                            or native_sequence < 0):
            problems.append(issue("table_event.sequence", pointer + "/source/sequence",
                                  "source.sequence must be null or a nonnegative integer"))
        _enum(source.get("attestation"), {"self_attested", "host_attested"},
              pointer + "/source/attestation", problems)

    principal = event.get("principal")
    principal_fields = {"id", "actor_id", "controller_id", "role"}
    if _closed_object(principal, pointer + "/principal", principal_fields,
                      principal_fields, problems):
        _string_id(principal.get("id"), pointer + "/principal/id", problems)
        _string_id(principal.get("actor_id"), pointer + "/principal/actor_id", problems,
                   nullable=True)
        _string_id(principal.get("controller_id"), pointer + "/principal/controller_id",
                   problems, nullable=True)
        _enum(principal.get("role"), _ROLES, pointer + "/principal/role", problems)

    integrity = event.get("integrity")
    integrity_fields = {"predecessor_digest", "event_digest", "checkpoint"}
    if _closed_object(integrity, pointer + "/integrity", integrity_fields,
                      integrity_fields, problems):
        _enum(integrity.get("checkpoint"),
              {"none", "same_writer", "externally_protected"},
              pointer + "/integrity/checkpoint", problems)
        _digest(integrity.get("predecessor_digest"),
                pointer + "/integrity/predecessor_digest", problems)
        _digest(integrity.get("event_digest"),
                pointer + "/integrity/event_digest", problems)
        attestation = source.get("attestation") if isinstance(source, dict) else None
        if attestation == "host_attested" and (
                integrity.get("checkpoint") != "externally_protected"
                or not isinstance(integrity.get("event_digest"), str)
                or _DIGEST.fullmatch(integrity.get("event_digest", "")) is None):
            problems.append(issue("table_event.host_integrity", pointer + "/integrity",
                                  "host-attested events require protected integrity"))
    if known_event_type:
        _validate_payload(event_type, event.get("payload"), pointer + "/payload", problems)
    elif not isinstance(event.get("payload"), dict):
        problems.append(issue("table_event.object", pointer + "/payload",
                              "payload must be an object"))


def _read_events(path):
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except (OSError, UnicodeError) as exc:
        raise InputValidationError([
            issue("input.unreadable", "/events", "event stream could not be read: %s" % exc)
        ]) from exc
    stripped = text.strip()
    if not stripped:
        return []
    if stripped.startswith("["):
        value = parse_json_value(stripped, "/events")
        if not isinstance(value, list):
            raise InputValidationError([
                issue("table_event.stream", "/events", "event stream must be an array or JSONL")
            ])
        return value
    values = []
    for line_number, line in enumerate(text.splitlines(), 1):
        if line.strip():
            values.append(parse_json_value(line, "/events/line-%d" % line_number))
    return values


def load_table_events(path):
    """Read, validate, and project one session's event stream."""
    events = _read_events(path)
    problems = []
    blockers = []
    for index, event in enumerate(events):
        _validate_event(event, index, problems, blockers)
    if problems:
        raise InputValidationError(problems)

    projection = TableEventProjection(event_count=len(events), blockers=blockers)
    current = [(index, event) for index, event in enumerate(events)
               if isinstance(event, dict)
               and event.get("schema_version") == TABLE_EVENT_SCHEMA_VERSION]
    supported = [event for _, event in current
                 if event.get("event_type") in KNOWN_EVENT_TYPES]
    if current:
        projection.campaign_id = current[0][1]["campaign_id"]
        projection.session_id = current[0][1]["session_id"]
        seen = set()
        expected = 1
        sequence_problems = []
        for index, event in current:
            pointer = "/events/%d" % index
            if event["campaign_id"] != projection.campaign_id:
                sequence_problems.append(issue("table_event.mixed_campaign", pointer + "/campaign_id",
                                               "one stream must contain one campaign"))
            if event["session_id"] != projection.session_id:
                sequence_problems.append(issue("table_event.mixed_session", pointer + "/session_id",
                                               "one stream must contain one session"))
            if event["event_id"] in seen:
                sequence_problems.append(issue("table_event.duplicate_event", pointer + "/event_id",
                                               "event_id must be unique"))
            seen.add(event["event_id"])
            if event["session_sequence"] != expected:
                sequence_problems.append(issue("table_event.sequence_gap",
                                               pointer + "/session_sequence",
                                               "expected contiguous session_sequence %d" % expected))
            expected += 1
        if sequence_problems:
            raise InputValidationError(sequence_problems)

    obligations_by_cause = {}
    for event in supported:
        if event["event_type"] == "narration.obligation":
            obligations_by_cause.setdefault(event.get("causation_id"), []).append(
                event["payload"])

    skipped = set()
    for event in supported:
        kind = event["event_type"]
        payload = event["payload"]
        ts = normalize_timestamp(event["occurred_at"])
        principal = event["principal"]
        author = principal.get("actor_id") or principal["id"]
        source_id = event["source"].get("native_id")
        common = {"ts": ts, "id": event["event_id"], "author": author,
                  "audience": list(event["audience"])}
        if source_id is not None:
            common["source_id"] = source_id
        if kind == "message.observed":
            row = dict(common, content=payload["content"], event_type=kind)
            if payload["content_redacted"]:
                projection.blockers.append(issue(
                    "table_event.content_redacted",
                    "/events/%s/payload/content" % event["event_id"],
                    "message content was redacted and cannot support a complete conduct evaluation"))
            if event["correlation_ids"]:
                row["correlation_id"] = list(event["correlation_ids"])
            projection.transcript.append(row)
        elif kind == "roll.observed":
            row = dict(common, content="%s roll result: %s" %
                       (payload["roll_kind"], payload["total"]),
                       event_type="roll.result", roll_id=event["event_id"])
            linked = [item["obligation_id"] for item
                      in obligations_by_cause.get(event["event_id"], [])
                      if item["kind"] == "consume_roll"]
            if len(linked) == 1:
                row["obligation_id"] = linked[0]
            elif len(linked) > 1:
                projection.blockers.append(issue(
                    "table_event.ambiguous_roll_obligation",
                    "/events/%s" % event["event_id"],
                    "one roll cannot project to multiple consume-roll obligations"))
            projection.transcript.append(row)
        elif kind == "narration.observed":
            row = dict(common, content=payload["content"], event_type=kind)
            if payload["resolves_obligation_ids"]:
                row["correlation_id"] = list(payload["resolves_obligation_ids"])
            projection.transcript.append(row)
        elif kind == "turn.started":
            projection.ledger.append({"ts": ts, "type": "turn",
                                      "id": event["event_id"],
                                      "event_id": event["event_id"],
                                      "actor": payload["actor_id"]})
        elif kind == "narration.obligation" and payload["kind"] == "narrate_event":
            projection.ledger.append({"ts": ts, "type": "event",
                                      "id": payload["obligation_id"],
                                      "event_id": payload["obligation_id"],
                                      "text": "narration obligation"})
        elif kind == "transport.gap":
            projection.blockers.append(issue(
                "table_event.transport_gap", "/events/%s" % event["event_id"],
                "transport reported missing evidence (%s -> %s)" %
                (payload["expected_sequence"], payload["observed_sequence"])))
        else:
            skipped.add(kind)
    projection.compatible_count = len(projection.transcript) + len(projection.ledger)
    projection.skipped_event_types = sorted(skipped)
    if projection.compatible_count == 0:
        projection.blockers.append(issue(
            "table_event.zero_compatible", "/events",
            "the stream contains no event type dmcheck can evaluate"))
    return projection


def evaluate_table_event_path(event_path, charter_path=None, gm=None,
                              dice_authors=None, mode="closed", now=None):
    """Evaluate a TableEvent stream without weakening incomplete evidence."""
    if mode not in ("closed", "live"):
        return invalid_result([
            issue("evaluation.mode", "/mode", "mode must be 'closed' or 'live'")
        ], mode=None)
    try:
        charter = load_charter(charter_path, gm=gm, dice_authors=dice_authors)
        projection = load_table_events(event_path)
    except InputValidationError as exc:
        return invalid_result(exc.issues, mode=mode)
    if projection.blockers:
        try:
            evaluation_ts = normalize_timestamp(now) if now is not None else None
        except InputValidationError as exc:
            return invalid_result(exc.issues, mode=mode, charter=charter)
        return EvaluationResult(
            "incomplete", mode, messages=len(projection.transcript),
            errors=projection.blockers, charter=charter,
            evaluation_ts=evaluation_ts)
    return evaluate(projection.transcript, charter, projection.ledger,
                    mode=mode, now=now)
