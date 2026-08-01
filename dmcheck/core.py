"""dmcheck core — deterministic conduct verdicts for live tabletop sessions.

Input: a transcript (+ optional engine-event ledger) and a versioned table
charter. Output: findings, each citing the charter rule it violates, with the
evidence span attached.

Design contract (D1–D5):
- A false accusation is the unforgivable bug: only source-observed evidence
  can become a definite finding. Legacy ambiguity is either silent or an
  explicitly inferred, low-confidence advisory and is never R8-promoted.
- Every finding cites its rule id and the charter values it was judged against.
- The charter is config, not code. The verdict path is model-free.
- No aggregate "DM score" exists anywhere in this package.
- dmcheck evaluates table conduct and communication, never action legality.
"""

from dataclasses import dataclass, field
import hashlib
import json
import re
import unicodedata

from .validation import (
    InputValidationError,
    ValidationIssue,
    issue,
    load_charter,
    load_ledger,
    load_transcript,
    normalize_charter,
    normalize_ledger,
    normalize_timestamp,
    normalize_transcript,
)

RULES = {
    "R1": "unanswered-player: a GM-directed question got no correlated response within threshold",
    "R2": "unconsumed-roll: a dice result got no correlated GM narration within threshold",
    "R3": "unnarrated-event: an engine event got no correlated GM narration",
    "R4": "missing-cue: a turn began and the next GM messages never addressed the actor by name",
    "R5": "retired compatibility id: actor/turn ownership never establishes action legality",
    "R6": "spoiler-leak: a configured hidden term appeared in a GM message",
    "R7": "dead-air: GM silence beyond threshold while a player waited",
    "R8": "unresolved-session-end: the session closes with open R1/R2/R3 obligations",
}

# R5 remains addressable so existing charters and agent integrations do not
# break, but it is intentionally absent from defaults and has no evaluator.
# Actor != turn owner is compatible with legal Reactions, Ready triggers,
# legendary/lair actions, controlled creatures, and other interrupts.  Any
# future authoritative decision event belongs to the portfolio contract;
# dmcheck must only evaluate the resulting conduct/communication obligation.
DEFAULT_RULES = tuple(rule for rule in RULES if rule != "R5")


# Loading and normalization are imported from ``validation``.  There is one
# packaged default and one parser regardless of checkout, wheel, CLI, MCP, or
# direct-library execution.


@dataclass
class EvaluationResult:
    """Typed repo-local evaluation outcome.

    ``__iter__`` preserves the historical ``findings, code = check(...)``
    adapter while direct callers can use ``status`` and ``to_dict()``.  Invalid
    and incomplete evidence never enters ``findings`` and always exits 2.
    """

    status: str
    mode: str
    messages: int = 0
    findings: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    skipped_rules: list = field(default_factory=list)
    eligible_rules: list = field(default_factory=list)
    charter: dict = None
    evaluation_ts: float = None

    @property
    def exit_code(self):
        return {"clean": 0, "findings": 1,
                "invalid": 2, "incomplete": 2}[self.status]

    def __iter__(self):
        yield self.findings
        yield self.exit_code

    def __len__(self):
        return 2

    def __getitem__(self, index):
        return (self.findings, self.exit_code)[index]

    def to_dict(self):
        counts = {
            rule: sum(1 for finding in self.findings
                      if finding.get("rule") == rule)
            for rule in sorted({finding.get("rule") for finding in self.findings
                                if finding.get("rule")})
        }
        metadata = None
        if self.charter is not None:
            metadata = {
                "schema_version": self.charter.get("schema_version"),
                "charter_version": self.charter.get("charter_version"),
                # The canonical validation digest commits to secret values and
                # is intentionally confined to the trusted charter document.
                # Publishing it would give ordinary output consumers an
                # offline dictionary oracle for low-entropy spoilers.
                "digest": public_charter_digest(self.charter),
                "digest_scope": "public-policy; hidden values withheld",
            }
        return {
            "result_schema_version": "1.0",
            "status": self.status,
            "exit_code": self.exit_code,
            "mode": self.mode,
            "evaluation_ts": self.evaluation_ts,
            "messages": self.messages,
            "findings": self.findings,
            "counts": counts,
            "errors": [error.to_dict() if isinstance(error, ValidationIssue)
                       else error for error in self.errors],
            "eligible_rules": list(self.eligible_rules),
            "skipped_rules": list(self.skipped_rules),
            "charter": metadata,
        }


def invalid_result(issues, mode="closed", messages=0, charter=None):
    """Build the same invalid envelope at any adapter boundary."""
    return EvaluationResult("invalid", mode, messages=messages,
                            errors=list(issues), charter=charter)


def _source_row_key(kind, row):
    """Stable repo-local source identity used for replay idempotency."""
    native = row.get("id") or row.get("source_id")
    if native is not None:
        return f"{kind}:id:{native}"
    payload = ({key: row.get(key) for key in ("ts", "author", "content")}
               if kind == "transcript" else dict(row))
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _dedupe_rows(rows, kind):
    unique = []
    seen = set()
    for row in rows:
        key = _source_row_key(kind, row)
        if key not in seen:
            seen.add(key)
            unique.append(row)
    return unique


# ---------- helpers ----------

def _finding_id(rule, evidence):
    # Transcript indices are presentation coordinates and may change when a
    # duplicate delivery is removed. The source timestamp/author/content stay
    # in evidence and form the stable repo-local correlation instead.
    identity_evidence = {
        k: v for k, v in evidence.items()
        if k not in {"index", "uncorrelated_gm_messages"}
    }
    # JSON distinguishes 10 from 10.0 textually even though both denote the
    # same instant. Batch loaders and watch normalize to floats, but callers
    # may invoke check() directly with integer timestamps; identity must not
    # depend on that transport detail.
    for key in ("ts", "ledger_ts"):
        value = identity_evidence.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            identity_evidence[key] = float(value)
    encoded = json.dumps(
        {"rule": rule, "evidence": identity_evidence}, sort_keys=True,
        separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return f"{rule.lower()}-{hashlib.sha256(encoded).hexdigest()[:24]}"


def _finding(rule, charter_cite, msg, evidence, **metadata):
    out = {"finding_id": _finding_id(rule, evidence),
           "rule": rule, "summary": RULES[rule], "charter": charter_cite,
           "detail": msg, "evidence": evidence}
    out.update(metadata)
    return out


def _dedupe_findings(findings):
    unique = {}
    for finding in findings:
        unique.setdefault(finding["finding_id"], finding)
    return list(unique.values())


def _ledger_evidence(event, charter, **evidence):
    """Bind a ledger-backed finding to the best available source identity."""
    # A fingerprint over raw prose is a dictionary oracle for configured
    # secrets even when the visible excerpt is redacted.  Bind to the
    # redacted event instead; native IDs remain intact when present.
    safe_event = _redact(event, charter)
    return {**evidence,
            "source_fingerprint": _source_row_key("ledger", safe_event)}


def _is_gm(r, ch):
    return r["author"] in ch["gm"]


def _is_dice(r, ch):
    return r["author"] in ch["dice_authors"]


def _is_ooc(r, ch):
    c = r["content"].lstrip()
    return any(c.startswith(m) for m in ch["ooc_markers"])


def _excerpt(r):
    return {"index": r["i"], "ts": r.get("ts"), "author": r["author"],
            "content": r["content"][:140]}


RULES_LEX = None


def _rules_lex():
    global RULES_LEX
    if RULES_LEX is None:
        RULES_LEX = re.compile(
            r"\b(dc|roll|check|save|damage|spell|slot|range|feet|ac|advantage|"
            r"disadvantage|initiative|hit points?|hp|attack|bonus action|"
            r"can i|do i|am i|what do (?:i|we) see)\b", re.I)
    return RULES_LEX


def _normal(value):
    return unicodedata.normalize("NFKC", str(value))


def _fold(value):
    """Unicode-compatible normalization for caseless security matching."""
    # Format controls such as zero-width joiners can make a protected word
    # look intact to a human while defeating literal matching.  They carry no
    # useful distinction at this security boundary, so compare without them.
    folded = "".join(c for c in _normal(value).casefold()
                     if unicodedata.category(c) != "Cf")
    return unicodedata.normalize("NFKC", folded)


def _word_in(term, text):
    # re.IGNORECASE does not perform full case folding (for example,
    # ``Straße`` does not match ``STRASSE``).  Fold first so the redaction
    # guard and the rule detector share the stronger comparison.
    return re.search(r"(?<![\w])" + re.escape(_fold(term)) + r"(?![\w])",
                     _fold(text)) is not None


def _hidden_terms(ch):
    """Return protected values with non-revealing, host-overridable IDs."""
    out = []
    for index, raw in enumerate(ch.get("hidden_terms") or [], 1):
        if isinstance(raw, dict):
            value = raw.get("value") or raw.get("term")
            secret_id = raw.get("id") or f"hidden-term-{index:04d}"
        else:
            value = raw
            secret_id = f"hidden-term-{index:04d}"
        if isinstance(value, str) and value:
            out.append((str(secret_id), value))
    return out


def _redact_text(value, ch):
    """Remove configured secrets, including case and Unicode variants.

    When normalization prevents a safe span-preserving substitution, redact
    the whole value.  Losing an excerpt is preferable to leaking a spoiler.
    """
    text = str(value)
    for secret_id, term in _hidden_terms(ch):
        pattern = re.compile(r"(?<![\w])" + re.escape(term) + r"(?![\w])", re.I)
        text = pattern.sub(f"[REDACTED:{secret_id}]", text)
        if _word_in(term, text):
            return "[REDACTED:configured-secret]"
    return text


def _redact(value, ch):
    if isinstance(value, str):
        return _redact_text(value, ch)
    if isinstance(value, list):
        return [_redact(v, ch) for v in value]
    if isinstance(value, tuple):
        return tuple(_redact(v, ch) for v in value)
    if isinstance(value, dict):
        return {(_redact_text(k, ch) if isinstance(k, str) else k): _redact(v, ch)
                for k, v in value.items()}
    return value


def public_charter(ch):
    """Return the effective charter safe for ordinary CLI/MCP/log output."""
    safe = {k: v for k, v in ch.items()
            if k not in {"hidden_terms", "charter_digest", "redaction_key",
                         "secret_key"}}
    safe["hidden_terms"] = [{"id": secret_id, "value": "[REDACTED]"}
                            for secret_id, _ in _hidden_terms(ch)]
    return _redact(safe, ch)


def redact_output(value, ch):
    """Public sink boundary: recursively scrub configured hidden values."""
    return _redact(value, ch)


def public_charter_digest(ch):
    """Digest only policy that is safe to expose to untrusted output sinks."""
    encoded = json.dumps(public_charter(ch), sort_keys=True,
                         separators=(",", ":"), ensure_ascii=False).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _identity(row):
    for key in ("obligation_id", "roll_id", "event_id", "id", "source_id"):
        value = row.get(key)
        if value is not None and str(value):
            return str(value)
    return None


def _references(row):
    refs = set()
    for key in ("reply_to", "correlation_id", "obligation_id", "roll_id"):
        value = row.get(key)
        if isinstance(value, (list, tuple, set)):
            refs.update(str(v) for v in value if v is not None and str(v))
        elif value is not None and str(value):
            refs.add(str(value))
    return refs


def _correlates(row, obligation_id):
    return obligation_id is not None and obligation_id in _references(row)


def _audience_state(row, transcript, ch):
    """Return (directed_to_gm, provenance).

    False means explicit public/player audience. None means the source omitted
    audience evidence and only a text heuristic is available.
    """
    event_type = str(row.get("event_type") or "").casefold()
    audience = row.get("audience")
    if audience is not None:
        values = audience if isinstance(audience, (list, tuple, set)) else [audience]
        targets = {_normal(v).casefold() for v in values}
        gm_names = {_normal(g).casefold() for g in ch.get("gm") or []}
        if targets & {"table", "public", "all", "everyone"}:
            return False, "observed"
        if targets & (gm_names | {"gm", "dm", "game_master"}):
            return True, "observed"
        return False, "observed"
    if event_type in {"question.to_gm", "question.to_dm"}:
        return True, "observed"
    if event_type in {"question.public", "question.to_table",
                      "question.to_player", "question.player"}:
        return False, "observed"
    reply_to = row.get("reply_to")
    if reply_to is not None:
        target = next((x for x in transcript
                       if str(x.get("id") or x.get("source_id") or "") == str(reply_to)),
                      None)
        if target is not None:
            return _is_gm(target, ch), "observed"
    return None, "inferred"


ROLL_RESULT_TYPES = {"roll.result", "dice.result", "vtt.roll_result",
                     "roll_result", "dice_roll_result"}
STATUS_EVENT_TYPES = {
    "bot.status", "bot.error", "dice.status", "dice.error", "roll.error",
    "status", "system", "error",
}
ROLL_NON_RESULT_TYPES = STATUS_EVENT_TYPES | {
    "roll.request", "dice.request", "vtt.roll_request", "roll_request",
    "dice_roll_request",
}
BOT_NON_RESULT = re.compile(
    r"(?:\b(?:error|invalid|usage|help|offline|online|ready|joined|rate.?limit|"
    r"permission|denied|cannot|could not|try again)\b|"
    r"\b(?:last|previous|recent)\s+rolls?\b|\broll\s+history\b)", re.I)
ROLL_RESULT = re.compile(
    r"(?:\broll(?:s|ed)?\b[^\n]{0,100}\b\d+d\d+\b[^\n]{0,100}"
    r"(?:=|total\s*:?)[ ]*-?\d+\b|"
    r"\b\d+d\d+(?:\s*[+\-]\s*\d+)?\b[^\n]{0,100}"
    r"(?:=|result\s*:?|total\s*:)[ ]*-?\d+\b)", re.I)


def _roll_result_state(row, ch):
    event_type = str(row.get("event_type") or "").casefold()
    if event_type in ROLL_NON_RESULT_TYPES:
        return False, None
    if BOT_NON_RESULT.search(row.get("content") or ""):
        return False, None
    if event_type in ROLL_RESULT_TYPES:
        return True, "observed"
    if not _is_dice(row, ch):
        return False, None
    if ROLL_RESULT.search(row.get("content") or ""):
        return True, "inferred"
    return False, None


def _correlates_response(row, obligation_id):
    """An exact reference on typed status/error output is not an answer."""
    event_type = str(row.get("event_type") or "").casefold()
    return event_type not in STATUS_EVENT_TYPES and _correlates(
        row, obligation_id)


def _correlates_narration(row, obligation_id):
    """Roll requests and status/error output are not narration evidence."""
    event_type = str(row.get("event_type") or "").casefold()
    return event_type not in ROLL_NON_RESULT_TYPES and _correlates(
        row, obligation_id)


def _effective_policy(rule, ch, finding):
    th = ch.get("thresholds") or {}
    if rule == "R1":
        return {"answer_within_messages": th.get("answer_within_messages", 6),
                "question_requires_gm_address":
                    ch.get("question_requires_gm_address", True),
                "correlation": "explicit_source_reference_when_available",
                "quiet_table_required_for_inferred": True}
    if rule == "R2":
        return {"roll_ack_within_messages": th.get("roll_ack_within_messages", 4),
                "dice_authors": list(ch.get("dice_authors") or []),
                "correlation": "explicit_source_reference_when_available",
                "text_only_result": "advisory"}
    if rule == "R3":
        return {"ledger_types": ["act", "event"],
                "correlation": "explicit_source_reference_when_available",
                "legacy_time_only": "advisory"}
    if rule == "R4":
        actor = (finding.get("evidence") or {}).get("actor")
        seat = ((ch.get("seats") or {}).get(actor) or {}) if actor else {}
        return {"cue_within_gm_messages": th.get("cue_within_gm_messages", 3),
                "cue_requires_mention": bool(seat.get("cue_requires_mention")),
                "mention_configured": bool(seat.get("mention")),
                "aliases": list(seat.get("aliases") or [])}
    if rule == "R5":
        return {"ledger_types": ["turn", "act"]}
    if rule == "R6":
        return {"hidden_term_ids": [secret_id for secret_id, _ in _hidden_terms(ch)],
                "matching": "unicode-normalized-whole-word-case-insensitive",
                "raw_evidence": "withheld"}
    if rule == "R7":
        return {"dead_air_seconds": th.get("dead_air_seconds", 300),
                "dead_air_requires_quiet_table":
                    ch.get("dead_air_requires_quiet_table", True),
                "quiet_table_max_messages": th.get("quiet_table_max_messages", 3),
                "dice_authors": list(ch.get("dice_authors") or []),
                "ooc_markers": list(ch.get("ooc_markers") or [])}
    if rule == "R8":
        return {"session_closed": True, "open_rule_ids": ["R1", "R2", "R3"],
                "source": "current_obligation_state"}
    return {}


def _finalize_findings(findings, ch):
    digest = public_charter_digest(ch)
    redacted = [_redact(raw, ch) for raw in findings]

    # Finding IDs are public correlation handles.  Computing them from raw
    # evidence would leak a deterministic hash of any configured spoiler in
    # that evidence.  Re-key every public ID from redacted evidence and repair
    # R8's references to the affected obligations before deriving its ID.
    remap = {}
    for raw, finding in zip(findings, redacted):
        if finding.get("rule") == "R8" or "rule" not in finding:
            continue
        public_id = _finding_id(finding["rule"], finding.get("evidence") or {})
        remap[raw.get("finding_id")] = public_id
        finding["finding_id"] = public_id
    for finding in redacted:
        if finding.get("rule") == "R8":
            evidence = finding.get("evidence") or {}
            refs = evidence.get("finding_ids")
            if isinstance(refs, list):
                evidence["finding_ids"] = [remap.get(ref, ref) for ref in refs]
            finding["finding_id"] = _finding_id("R8", evidence)

    out = []
    for finding in redacted:
        if "rule" not in finding:
            out.append(finding)
            continue
        finding.setdefault("status", "open")
        finding.setdefault("severity", "finding")
        finding.setdefault("provenance", "observed")
        finding.setdefault(
            "confidence",
            "high" if (finding["provenance"] == "observed"
                       and finding["severity"] == "finding") else "low")
        finding["effective_policy"] = _effective_policy(finding["rule"], ch,
                                                         finding)
        finding["charter_version"] = ch.get("charter_version", "unversioned")
        finding["charter_digest"] = digest
        finding["charter_digest_scope"] = "public-policy; hidden values withheld"
        out.append(_redact(finding, ch))
    return out


def _r4_context(transcript, charter, event, required_beats):
    gms_after = [r for r in transcript
                 if _is_gm(r, charter) and r.get("ts") is not None
                 and r["ts"] >= event["ts"]][:required_beats]
    seat = (charter.get("seats") or {}).get(event["actor"]) or {}
    names = [event["actor"]] + list(seat.get("aliases") or [])
    if seat.get("cue_requires_mention") and seat.get("mention"):
        cued = any(seat["mention"] in r["content"] for r in gms_after)
        how = (f"contain the required mention {seat['mention']!r} "
               f"(this seat's transport drops name-in-prose)")
    else:
        cued = any(_word_in(name, row["content"])
                   for name in names for row in gms_after)
        how = ("address them by name or alias"
               if len(names) > 1 else "address them by name")
    return gms_after, cued, how


def _r7_yielded_floor(transcript, charter, row, end_ts):
    if not charter.get("dead_air_requires_quiet_table", True):
        return False
    between = [item for item in transcript[row["i"] + 1:]
               if item.get("ts") is not None
               and (end_ts is None or item["ts"] < end_ts)
               and not _is_gm(item, charter) and not _is_dice(item, charter)]
    return len(between) >= charter["thresholds"].get(
        "quiet_table_max_messages", 3)


def incomplete_obligations(transcript, charter, ledger=None, now=None):
    """Return terminal obligations that lack enough evidence for a finding.

    This is deliberately top-level state, never a conduct finding. It covers
    the repo-local R4/R7 evidence windows corrected by DMC-002; canonical
    cross-process obligation state remains a PORT-001/PORT-002 dependency.
    """
    ledger = ledger or []
    enabled = set(charter.get("rules_enabled", list(RULES)))
    thresholds = charter["thresholds"]
    incomplete = []

    if "R4" in enabled:
        required = thresholds.get("cue_within_gm_messages", 3)
        for event in ledger:
            if event.get("type") != "turn" or not event.get("actor") \
                    or event.get("ts") is None:
                continue
            window, cued, _ = _r4_context(
                transcript, charter, event, required)
            if not cued and len(window) < required:
                evidence = _ledger_evidence(
                    event, charter, actor=event["actor"],
                    ledger_ts=event["ts"])
                incomplete.append({
                    "finding_id": _finding_id("R4", evidence),
                    "rule": "R4", "status": "incomplete",
                    "reason": "insufficient_qualifying_gm_beats",
                    "charter": f"cue_within_gm_messages={required}",
                    "observed_gm_beats": len(window),
                    "required_gm_beats": required,
                    "evidence": evidence,
                })

    if "R7" in enabled:
        limit = thresholds.get("dead_air_seconds", 300)
        for row in transcript:
            if _is_gm(row, charter) or _is_dice(row, charter) \
                    or _is_ooc(row, charter) or row.get("ts") is None:
                continue
            next_gm = next((item for item in transcript[row["i"] + 1:]
                           if _is_gm(item, charter)
                           and item.get("ts") is not None), None)
            if next_gm is not None or _r7_yielded_floor(
                    transcript, charter, row, now):
                continue
            observed = None if now is None else max(0, now - row["ts"])
            if observed is None or observed <= limit:
                evidence = _excerpt(row)
                incomplete.append({
                    "finding_id": _finding_id("R7", evidence),
                    "rule": "R7", "status": "incomplete",
                    "reason": "insufficient_elapsed_observation",
                    "charter": f"dead_air_seconds={limit}",
                    "observed_seconds": (None if observed is None
                                         else int(observed)),
                    "required_seconds": limit,
                    "evidence": evidence,
                })

    unique = {}
    for item in incomplete:
        unique.setdefault(item["finding_id"], item)
    return list(unique.values())


# ---------- the rules ----------

def _run_rules(transcript, charter, ledger=None, closed=True, now=None):
    ch = charter
    ledger = ledger or []
    T = transcript
    th = ch["thresholds"]
    findings = []
    enabled = set(ch.get("rules_enabled", list(DEFAULT_RULES)))
    # R1 unanswered-player: a non-GM, non-dice GM-directed question with no
    # correlated response in the next `answer_within_messages` messages.
    if "R1" in enabled:
        n = th.get("answer_within_messages", 6)
        for r in T:
            typed_question = str(r.get("event_type") or "").casefold() in {
                "question.to_gm", "question.to_dm",
            }
            if (_is_gm(r, ch) or _is_dice(r, ch)
                    or ("?" not in r["content"] and not typed_question)):
                continue
            audience, audience_provenance = _audience_state(r, T, ch)
            if audience is False:
                # Explicit public/player-directed questions are not GM
                # obligations.  Do not override source audience with prose.
                continue
            # v0.4 evidence bar 1: the question must carry GM-directed
            # evidence (GM named, rules/world lexicon, or adjacency to a GM
            # beat). Ground truth: only 43% of question-marked player lines
            # are TO_GM; a bare "?" is a false-accusation machine.
            if audience is None and ch.get("question_requires_gm_address", True):
                prev = T[r["i"] - 1] if r["i"] > 0 else None
                directed = (any(_word_in(g, r["content"]) for g in ch["gm"])
                            or _rules_lex().search(r["content"])
                            or (prev is not None and _is_gm(prev, ch)))
                if not directed:
                    continue
            after = T[r["i"] + 1:]
            window = after[:n]
            complete = len(window) >= n or closed
            obligation_id = _identity(r)
            explicit = audience is True and audience_provenance == "observed" \
                and obligation_id is not None
            # v0.4 evidence bar 2: the table must actually be WAITING - if
            # other players carry on, an inferred text-only beat was not
            # provably blocked.  An observed, ID-bearing obligation remains
            # open through unrelated concurrent player activity.
            others = [x for x in window
                      if x["author"] != r["author"] and not _is_gm(x, ch)
                      and not _is_dice(x, ch) and not _is_ooc(x, ch)]
            if others and not explicit:
                continue
            # The threshold decides when the obligation becomes overdue; a
            # later explicit response still heals its current OPEN state.
            correlated = any(_is_gm(x, ch)
                             and _correlates_response(x, obligation_id)
                             for x in after)
            if correlated or not complete:
                continue
            gm_present = any(_is_gm(x, ch) for x in after)
            # Preserve the calibrated empty-tail guard for text-only evidence.
            if not explicit and not window:
                continue
            if explicit:
                detail = (f"GM-directed question from {r['author']} got no "
                          f"correlated GM response within {n} messages")
                severity, status, provenance = "finding", "open", "observed"
            else:
                # Without a source ID, an intervening GM message may or may
                # not be an answer.  D1 requires silence, not an accusation;
                # fail-closed coverage reporting belongs to DMC-001.
                if gm_present:
                    continue
                detail = (f"possible GM-directed question from {r['author']} "
                          f"has no provably correlated GM response within {n} "
                          "messages; source audience/correlation evidence is "
                          "incomplete")
                severity = "advisory"
                status = "open"
                provenance = "inferred"
            evidence = _excerpt(r)
            evidence.update({"obligation_id": obligation_id,
                             "audience_evidence": audience_provenance,
                             "uncorrelated_gm_messages":
                                 sum(1 for x in window if _is_gm(x, ch))})
            findings.append(_finding(
                "R1", f"answer_within_messages={n}; correlation=explicit",
                detail, evidence, severity=severity, status=status,
                provenance=provenance))

    # R2 unconsumed-roll: a typed or conservatively recognized dice result
    # with no correlated GM narration in the configured window.
    if "R2" in enabled:
        n = th.get("roll_ack_within_messages", 4)
        for r in T:
            # A typed result authored by the GM is already table-visible GM
            # narration, not an unconsumed player/dice-bot obligation.
            if _is_gm(r, ch):
                continue
            is_result, provenance = _roll_result_state(r, ch)
            if not is_result:
                continue
            after = T[r["i"] + 1:]
            window = after[:n]
            tail_is_end = closed and (r["i"] + 1 + n > len(T))
            complete = len(window) >= n or closed
            if not ((window and complete) or tail_is_end):
                continue
            obligation_id = _identity(r)
            if any(_is_gm(x, ch) and _correlates_narration(x, obligation_id)
                   for x in after):
                continue
            gm_present = any(_is_gm(x, ch) for x in after)
            explicit = provenance == "observed" and obligation_id is not None
            if explicit:
                detail = (f"dice result from {r['author']} received no correlated "
                          f"GM narration within {n} messages")
                severity, status = "finding", "open"
            else:
                # A legacy text export cannot prove whether this GM message
                # narrates the roll.  Preserve D1 silence; DMC-001 exposes the
                # resulting coverage gap at the evaluation-envelope level.
                if gm_present:
                    continue
                detail = (f"possible dice result from {r['author']} has no "
                          f"provably correlated GM narration within {n} messages; "
                          "text-only or correlation evidence is incomplete")
                severity = "advisory"
                status = "open"
            evidence = _excerpt(r)
            evidence.update({"obligation_id": obligation_id,
                             "uncorrelated_gm_messages":
                                 sum(1 for x in window if _is_gm(x, ch))})
            findings.append(_finding(
                "R2", f"roll_ack_within_messages={n}; correlation=explicit",
                detail, evidence, severity=severity, status=status,
                provenance=provenance))

    # R3 unnarrated-event: an explicit source reference is the only evidence
    # that narration closes a typed event.  Time-only legacy evidence remains
    # visible as advisory/uncertain instead of being silently "healed" by
    # unrelated GM chatter.
    if "R3" in enabled and ledger:
        for e in ledger:
            if e.get("type") not in ("event", "act"):
                continue
            obligation_id = _identity(e)
            if obligation_id is None and e.get("ts") is None:
                # With neither identity nor time there is no observable join
                # key.  DMC-001's evaluation envelope reports this gap.
                continue
            later_gms = [
                r for r in T
                if _is_gm(r, ch)
                and (e.get("ts") is None or r.get("ts") is None
                     or r["ts"] >= e["ts"])
            ]
            if any(_correlates_narration(r, obligation_id)
                   for r in later_gms):
                continue
            explicit = obligation_id is not None
            if explicit:
                # In live mode the event itself opens an obligation, not an
                # immediate accusation. Give the GM at least one subsequent
                # narration opportunity; the first uncorrelated GM beat makes
                # the missing correlation observable. Closed evaluation can
                # adjudicate an unnarrated tail directly.
                if not closed and not later_gms:
                    continue
                detail = ("engine event received no correlated GM narration: "
                          f"{e.get('text') or e.get('type')}")
                severity, status, provenance = "finding", "open", "observed"
            else:
                detail = ("engine event has no provably correlated GM narration; "
                          "legacy time-only ledger evidence is non-authoritative")
                severity = "advisory"
                status = "uncertain" if later_gms else "open"
                provenance = "inferred"
            findings.append(_finding(
                "R3", "correlation=explicit_source_reference",
                detail,
                    _ledger_evidence(
                        e, ch, ledger_ts=e.get("ts"), event_id=obligation_id,
                    type=e.get("type"),
                    text=(e.get("text") or "")[:140],
                    uncorrelated_gm_messages=len(later_gms)),
                severity=severity, status=status, provenance=provenance))

    # R4 missing-cue: after a `turn` ledger event, none of the next
    # `cue_within_gm_messages` GM messages names the actor.
    if "R4" in enabled and ledger:
        k = th.get("cue_within_gm_messages", 3)
        for e in ledger:
            if e.get("type") != "turn" or not e.get("actor") or e.get("ts") is None:
                continue
            # per-seat delivery policy is shared with terminal-incomplete
            # reporting. Crucially, fewer than k qualifying GM beats are an
            # open evidence window, not a close-time shortcut to accusation.
            gms_after, cued, how = _r4_context(T, ch, e, k)
            if len(gms_after) == k and not cued:
                findings.append(_finding(
                    "R4", f"cue_within_gm_messages={k}",
                    f"turn began for {e['actor']} but the next {k} GM "
                    f"message(s) never {how}",
                    _ledger_evidence(
                        e, ch, actor=e["actor"], ledger_ts=e["ts"])))

    # R5 deliberately has no evaluator. Actor != turn owner is coordination
    # context, not evidence that an action was illegal or that anyone violated
    # table procedure. Do not infer an authority decision from today's lossy
    # ledger.

    # R6 spoiler-leak: hidden terms in GM messages (whole word).
    if "R6" in enabled:
        for r in T:
            if not _is_gm(r, ch):
                continue
            leaks = [secret_id for secret_id, term in _hidden_terms(ch)
                     if _word_in(term, r["content"])]
            if leaks:
                findings.append(_finding(
                    "R6", "hidden_term_ids=" + ",".join(leaks),
                    "configured hidden term appeared in a GM message; raw "
                    "content withheld",
                    {"index": r["i"], "author": r["author"],
                     "secret_ids": leaks, "content": "[REDACTED]"},
                    redacted=True))

    # R7 dead-air: a player message followed by a measurable GM gap beyond
    # threshold (needs timestamps plus a GM response or evaluation horizon).
    if "R7" in enabled:
        limit = th.get("dead_air_seconds", 300)
        for r in T:
            if _is_gm(r, ch) or _is_dice(r, ch) or _is_ooc(r, ch) or r["ts"] is None:
                continue
            nxt = next((x for x in T[r["i"] + 1:]
                        if _is_gm(x, ch) and x.get("ts") is not None), None)
            gap = (nxt["ts"] - r["ts"]) if nxt else \
                  ((now - r["ts"]) if now is not None else None)
            if gap is None or gap <= limit:
                continue
            # v0.4 yielded-floor exemption: a GM holding back while players
            # talk is craft, not absence. Dead air requires a QUIET table.
            end_ts = nxt["ts"] if nxt else now
            if _r7_yielded_floor(T, ch, r, end_ts):
                continue
            if nxt:
                lead = f"GM took {int(gap)}s to respond after "
            elif closed:
                lead = f"GM silent for {int(gap)}s at session close after "
            else:
                lead = f"GM silent for {int(gap)}s (ongoing) after "
            findings.append(_finding(
                "R7", f"dead_air_seconds={limit}",
                lead + f"{r['author']}'s message", _excerpt(r)))

    # Duplicate delivery of the same source obligation is idempotent. Do this
    # before R8 so session-end counts cannot be inflated by redelivery.
    findings = _dedupe_findings(findings)

    # R8 unresolved-session-end: derive from the current obligation state, not
    # an arbitrary transcript quartile.  Uncertain legacy inferences remain
    # visible above but are not promoted to a definite open obligation here.
    if "R8" in enabled and closed and findings:
        open_obligations = [f for f in findings
                            if f["rule"] in ("R1", "R2", "R3")
                            and f.get("status", "open") == "open"
                            and f.get("provenance") == "observed"]
        if open_obligations:
            findings.append(_finding(
                "R8", "session_closed=true; source=current_obligation_state",
                f"session ends with {len(open_obligations)} open obligation(s): "
                + ", ".join(sorted({f['rule'] for f in open_obligations})),
                {"open": [f["rule"] for f in open_obligations],
                 "obligation_ids": [f.get("evidence", {}).get("obligation_id")
                                    or f.get("evidence", {}).get("event_id")
                                    for f in open_obligations],
                 "finding_ids": [f["finding_id"] for f in open_obligations]}))

    findings = _finalize_findings(findings, ch)
    findings = _dedupe_findings(findings)
    return findings, (1 if findings else 0)


def _skip(rule, code, message):
    return {"rule": rule, "code": code, "message": message}


def _eligible_rules(transcript, charter, ledger, mode, now):
    enabled = charter["rules_enabled"]
    eligible = []
    skipped = []
    has_messages = bool(transcript)
    player_messages = [row for row in transcript
                       if row["content"].strip()
                       and not _is_gm(row, charter)
                       and not _is_dice(row, charter)
                       and not _is_ooc(row, charter)]
    dice_messages = [row for row in transcript
                     if row["content"].strip() and _is_dice(row, charter)]
    gm_messages = [row for row in transcript if _is_gm(row, charter)]
    gm_timestamps = [row["ts"] for row in gm_messages
                     if row["ts"] is not None]

    for rule in enabled:
        reason = None
        if rule == "R8":
            continue
        if rule == "R1":
            if player_messages:
                eligible.append(rule)
            else:
                reason = _skip(rule, "player_messages.unavailable",
                               "R1 requires at least one player message")
        elif rule == "R2":
            if dice_messages:
                eligible.append(rule)
            else:
                reason = _skip(rule, "dice_messages.unavailable",
                               "R2 requires at least one configured dice-author message")
        elif rule == "R3":
            events = [event for event in ledger
                      if event.get("type") in ("event", "act")
                      and event.get("ts") is not None]
            last_gm = gm_messages[-1] if gm_messages else None
            if events and last_gm is not None and last_gm["ts"] is not None:
                eligible.append(rule)
            else:
                reason = _skip(rule, "timestamped_ledger.unavailable",
                               "R3 requires a timestamped event and GM message")
        elif rule == "R4":
            turns = [event for event in ledger if event.get("type") == "turn"
                     and event.get("actor") and event.get("ts") is not None]
            compatible = any(gm["ts"] >= turn["ts"]
                             for turn in turns for gm in gm_messages
                             if gm["ts"] is not None)
            if compatible and len(gm_timestamps) == len(gm_messages):
                eligible.append(rule)
            else:
                reason = _skip(rule, "timestamped_turn.unavailable",
                               "R4 requires a timestamped turn and GM message")
        elif rule == "R5":
            reason = _skip(
                rule, "rule.retired",
                "R5 is retired because actor/turn ownership cannot establish "
                "action legality")
        elif rule == "R6":
            if has_messages and charter["hidden_terms"]:
                eligible.append(rule)
            else:
                reason = _skip(rule, "hidden_terms.unavailable",
                               "R6 requires messages and configured hidden terms")
        elif rule == "R7":
            compatible = False
            for row in transcript:
                if (row["ts"] is None or _is_gm(row, charter)
                        or _is_dice(row, charter) or _is_ooc(row, charter)):
                    continue
                next_gm = next((candidate for candidate
                                in transcript[row["i"] + 1:]
                                if _is_gm(candidate, charter)), None)
                if ((next_gm is not None and next_gm["ts"] is not None)
                        or (next_gm is None and now is not None)):
                    compatible = True
                    break
            if compatible:
                eligible.append(rule)
            else:
                reason = _skip(rule, "timestamped_messages.unavailable",
                               "R7 requires timestamped player and GM/live-clock evidence")
        if reason is not None:
            skipped.append(reason)
    if "R8" in enabled:
        bases = {"R1", "R2", "R3"}.intersection(eligible)
        if mode == "closed" and bases:
            eligible.append("R8")
        else:
            skipped.append(_skip(
                "R8", "closed_tail.unavailable",
                "R8 requires closed mode and an eligible R1, R2, or R3"))
    return eligible, skipped


def evaluate(transcript, charter, ledger=None, mode="closed", now=None):
    """Normalize, validate, establish evidence eligibility, then run rules.

    This is the canonical direct API.  It never raises for caller-controlled
    data: invalid input and insufficient evidence are explicit status values.
    """
    if mode not in ("closed", "live"):
        return invalid_result([
            issue("evaluation.mode", "/mode", "mode must be 'closed' or 'live'")
        ], mode=None)

    problems = []
    normalized_charter = None
    normalized_transcript = None
    normalized_ledger = None
    normalized_now = None
    try:
        normalized_charter = normalize_charter(charter, require_gm=True)
    except InputValidationError as exc:
        problems.extend(exc.issues)
    try:
        normalized_transcript = normalize_transcript(transcript)
    except InputValidationError as exc:
        problems.extend(exc.issues)
    try:
        normalized_ledger = normalize_ledger(ledger)
    except InputValidationError as exc:
        problems.extend(exc.issues)
    if now is not None:
        try:
            normalized_now = normalize_timestamp(now)
        except InputValidationError as exc:
            problems.extend(exc.issues)
    if problems:
        return invalid_result(problems, mode=mode,
                              messages=len(normalized_transcript or []),
                              charter=normalized_charter)

    normalized_transcript = _dedupe_rows(normalized_transcript, "transcript")
    for index, row in enumerate(normalized_transcript):
        row["i"] = index
    normalized_ledger = _dedupe_rows(normalized_ledger, "ledger")

    incomplete = []
    if not normalized_transcript:
        incomplete.append(issue("transcript.empty", "/transcript",
                                "no usable transcript messages were supplied"))
    elif not any(row["content"].strip() for row in normalized_transcript):
        incomplete.append(issue(
            "transcript.no_effective_content", "/transcript",
            "the transcript contains no nonempty message content"))
    elif (not any(_is_gm(row, normalized_charter)
                  for row in normalized_transcript)
          and not (normalized_now is not None
                   and "R7" in normalized_charter["rules_enabled"])):
        incomplete.append(issue(
            "evidence.gm_unobserved", "/transcript",
            "none of the configured GM authors appears in the transcript"))

    eligible, skipped = _eligible_rules(
        normalized_transcript, normalized_charter, normalized_ledger,
        mode, normalized_now)
    if not eligible:
        incomplete.append(issue(
            "evidence.no_eligible_rules", "/rules_enabled",
            "no enabled rule has the evidence required to evaluate"))
    if incomplete:
        return EvaluationResult(
            "incomplete", mode, messages=len(normalized_transcript),
            errors=incomplete, skipped_rules=skipped,
            eligible_rules=eligible, charter=normalized_charter,
            evaluation_ts=normalized_now)

    try:
        effective_charter = dict(normalized_charter)
        effective_charter["rules_enabled"] = list(eligible)
        findings, _ = _run_rules(
            normalized_transcript, effective_charter, normalized_ledger,
            closed=(mode == "closed"), now=normalized_now)
    except Exception:  # fail closed at the public boundary; never leak a traceback
        return invalid_result([
            issue("evaluation.failed", "/evaluation",
                  "evaluation could not complete for the supplied input")
        ], mode=mode, messages=len(normalized_transcript),
                              charter=normalized_charter)
    return EvaluationResult(
        "findings" if findings else "clean", mode,
        messages=len(normalized_transcript), findings=findings,
        skipped_rules=skipped, eligible_rules=eligible,
        charter=normalized_charter, evaluation_ts=normalized_now)


def check(transcript, charter, ledger=None, closed=True, now=None, mode=None):
    """Backward-compatible adapter around :func:`evaluate`.

    New callers should pass ``mode='closed'`` or ``mode='live'`` explicitly.
    Existing callers using ``closed=`` still receive an iterable typed result.
    """
    if mode is None:
        if not isinstance(closed, bool):
            return invalid_result([
                issue("evaluation.mode", "/closed", "closed must be a boolean")
            ])
        mode = "closed" if closed else "live"
    return evaluate(transcript, charter, ledger, mode=mode, now=now)


def evaluate_paths(transcript_path, charter_path=None, ledger_path=None,
                   gm=None, dice_authors=None, mode="closed", now=None):
    """Canonical file adapter shared by CLI and MCP."""
    if mode not in ("closed", "live"):
        return invalid_result([
            issue("evaluation.mode", "/mode", "mode must be 'closed' or 'live'")
        ], mode=None)
    try:
        charter = load_charter(charter_path, gm=gm,
                               dice_authors=dice_authors)
        transcript = load_transcript(transcript_path)
        ledger = load_ledger(ledger_path)
    except InputValidationError as exc:
        return invalid_result(exc.issues, mode=mode)
    return evaluate(transcript, charter, ledger, mode=mode, now=now)
