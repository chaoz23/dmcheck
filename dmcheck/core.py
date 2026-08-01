"""dmcheck core — deterministic conduct verdicts for live tabletop sessions.

Input: a transcript (+ optional engine-event ledger) and a versioned table
charter. Output: findings, each citing the charter rule it violates, with the
evidence span attached.

Design contract (D1–D5):
- A false accusation is the unforgivable bug: a rule fires only when the
  transcript/ledger *provably* shows the violation. Ambiguity produces silence.
- Every finding cites its rule id and the charter values it was judged against.
- The charter is config, not code. The verdict path is model-free.
- No aggregate "DM score" exists anywhere in this package.
- dmcheck evaluates table conduct and communication, never action legality.
"""

from dataclasses import dataclass, field

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
    "R1": "unanswered-player: a player question to the table got no GM response within threshold",
    "R2": "unconsumed-roll: a dice result was never followed by any GM message",
    "R3": "unnarrated-event: an engine event newer than the last GM message (never told to the table)",
    "R4": "missing-cue: a turn began and the next GM messages never addressed the actor by name",
    "R5": "retired compatibility id: actor/turn ownership never establishes action legality",
    "R6": "spoiler-leak: a configured hidden term appeared in a GM message",
    "R7": "dead-air: GM silence beyond threshold while a player waited",
    "R8": "unresolved-session-end: the transcript ends with open R1/R2/R3 findings in its tail",
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
                "digest": self.charter.get("charter_digest"),
            }
        return {
            "result_schema_version": "1.0",
            "status": self.status,
            "exit_code": self.exit_code,
            "mode": self.mode,
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


# ---------- helpers ----------

def _finding(rule, charter_cite, msg, evidence):
    return {"rule": rule, "summary": RULES[rule], "charter": charter_cite,
            "detail": msg, "evidence": evidence}


def _is_gm(r, ch):
    return r["author"] in ch["gm"]


def _is_dice(r, ch):
    return r["author"] in ch["dice_authors"]


def _is_ooc(r, ch):
    c = r["content"].lstrip()
    return any(c.startswith(m) for m in ch["ooc_markers"])


def _excerpt(r):
    return {"index": r["i"], "author": r["author"],
            "content": r["content"][:140]}


RULES_LEX = None


def _rules_lex():
    global RULES_LEX
    if RULES_LEX is None:
        import re
        RULES_LEX = re.compile(
            r"\b(dc|roll|check|save|damage|spell|slot|range|feet|ac|advantage|"
            r"disadvantage|initiative|hit points?|hp|attack|bonus action|"
            r"can i|do i|am i|what do (?:i|we) see)\b", re.I)
    return RULES_LEX


def _word_in(term, text):
    import re
    return re.search(r"(?<![A-Za-z0-9_])" + re.escape(term) + r"(?![A-Za-z0-9_])",
                     text, re.I) is not None


# ---------- the rules ----------

def _run_rules(transcript, charter, ledger=None, closed=True, now=None):
    ch = charter
    ledger = ledger or []
    T = transcript
    th = ch["thresholds"]
    findings = []
    enabled = set(ch.get("rules_enabled", list(DEFAULT_RULES)))
    gm_idx = [r["i"] for r in T if _is_gm(r, ch)]
    # R1 unanswered-player: a non-GM, non-dice message containing a question,
    # with NO GM message in the next `answer_within_messages` messages.
    if "R1" in enabled:
        n = th.get("answer_within_messages", 6)
        for r in T:
            if _is_gm(r, ch) or _is_dice(r, ch) or "?" not in r["content"]:
                continue
            # v0.4 evidence bar 1: the question must carry GM-directed
            # evidence (GM named, rules/world lexicon, or adjacency to a GM
            # beat). Ground truth: only 43% of question-marked player lines
            # are TO_GM; a bare "?" is a false-accusation machine.
            if ch.get("question_requires_gm_address", True):
                prev = T[r["i"] - 1] if r["i"] > 0 else None
                directed = (any(_word_in(g, r["content"]) for g in ch["gm"])
                            or _rules_lex().search(r["content"])
                            or (prev is not None and _is_gm(prev, ch)))
                if not directed:
                    continue
            window = [x for x in T[r["i"] + 1: r["i"] + 1 + n]]
            complete = len(window) >= n or closed
            # v0.4 evidence bar 2: the table must actually be WAITING - if
            # other players carry on, the beat was not blocked (10% of true
            # GM-directed questions go unanswered even at pro tables).
            others = [x for x in window
                      if x["author"] != r["author"] and not _is_gm(x, ch)
                      and not _is_dice(x, ch) and not _is_ooc(x, ch)]
            if others:
                continue
            if window and complete and not any(_is_gm(x, ch) for x in window):
                findings.append(_finding(
                    "R1", f"answer_within_messages={n} (directed, table waiting)",
                    f"GM-directed question from {r['author']} got no GM response "
                    f"within {n} messages while the table waited",
                    _excerpt(r)))

    # R2 unconsumed-roll: a dice-author message with NO GM message at all in
    # the next `roll_ack_within_messages` messages (or before transcript end).
    if "R2" in enabled:
        n = th.get("roll_ack_within_messages", 4)
        for r in T:
            if not _is_dice(r, ch):
                continue
            window = T[r["i"] + 1: r["i"] + 1 + n]
            tail_is_end = closed and (r["i"] + 1 + n > len(T))
            complete = len(window) >= n or closed
            if not any(_is_gm(x, ch) for x in window) \
                    and ((window and complete) or tail_is_end):
                findings.append(_finding(
                    "R2", f"roll_ack_within_messages={n}",
                    f"dice result from {r['author']} never followed by a GM message",
                    _excerpt(r)))

    # R3 unnarrated-event: ledger events strictly after the last GM message.
    if "R3" in enabled and ledger and gm_idx:
        last_gm = next((r for r in reversed(T) if _is_gm(r, ch)))
        last_gm_ts = last_gm["ts"]
        if last_gm_ts is not None:
            for e in ledger:
                if e.get("type") in ("event", "act") and e.get("ts") is not None \
                        and e["ts"] > last_gm_ts:
                    findings.append(_finding(
                        "R3", "ledger vs last GM message",
                        f"engine event never narrated: {e.get('text') or e.get('type')}",
                        {"ledger_ts": e["ts"], "text": (e.get("text") or "")[:140]}))

    # R4 missing-cue: after a `turn` ledger event, none of the next
    # `cue_within_gm_messages` GM messages names the actor.
    if "R4" in enabled and ledger:
        k = th.get("cue_within_gm_messages", 3)
        for e in ledger:
            if e.get("type") != "turn" or not e.get("actor") or e.get("ts") is None:
                continue
            gms_after = [r for r in T if _is_gm(r, ch)
                         and r["ts"] is not None and r["ts"] >= e["ts"]][:k]
            # per-seat cue policy (v0.3): a seat behind a mention-gated
            # transport only RECEIVES a cue if the literal mention string is
            # present — name-in-prose is not deliverable to it. Without a
            # configured mention string the requirement is unverifiable, so
            # fall back to name matching (D1: never accuse on ambiguity).
            seat = (ch.get("seats") or {}).get(e["actor"]) or {}
            names = [e["actor"]] + list(seat.get("aliases") or [])
            if seat.get("cue_requires_mention") and seat.get("mention"):
                cued = any(seat["mention"] in r["content"] for r in gms_after)
                how = (f"contain the required mention {seat['mention']!r} "
                       f"(this seat's transport drops name-in-prose)")
            else:
                cued = any(_word_in(nm, r["content"])
                           for nm in names for r in gms_after)
                how = ("address them by name or alias"
                       if len(names) > 1 else "address them by name")
            if gms_after and not cued:
                findings.append(_finding(
                    "R4", f"cue_within_gm_messages={k}",
                    f"turn began for {e['actor']} but the next {len(gms_after)} GM "
                    f"message(s) never {how}",
                    {"actor": e["actor"], "ledger_ts": e["ts"]}))

    # R5 deliberately has no evaluator. Actor != turn owner is coordination
    # context, not evidence that an action was illegal or that anyone violated
    # table procedure. Do not infer an authority decision from today's lossy
    # ledger.

    # R6 spoiler-leak: hidden terms in GM messages (whole word).
    if "R6" in enabled:
        for r in T:
            if not _is_gm(r, ch):
                continue
            leaks = [t for t in ch["hidden_terms"] if _word_in(t, r["content"])]
            if leaks:
                findings.append(_finding(
                    "R6", "hidden_terms", f"hidden term(s) {leaks} in a GM message",
                    _excerpt(r)))

    # R7 dead-air: a player message followed by a measurable GM gap beyond
    # threshold (needs timestamps; both bounding messages must exist).
    if "R7" in enabled:
        limit = th.get("dead_air_seconds", 300)
        for r in T:
            if _is_gm(r, ch) or _is_dice(r, ch) or _is_ooc(r, ch) or r["ts"] is None:
                continue
            # The first subsequent GM beat bounds the wait.  If that beat has
            # no timestamp, a later timestamped beat cannot safely stand in
            # for it without risking a false accusation.
            nxt = next((x for x in T[r["i"] + 1:] if _is_gm(x, ch)), None)
            gap = (nxt["ts"] - r["ts"]) if nxt and nxt["ts"] is not None else \
                  ((now - r["ts"]) if (now is not None and not closed) else None)
            if gap is None or gap <= limit:
                continue
            # v0.4 yielded-floor exemption: a GM holding back while players
            # talk is craft, not absence. Dead air requires a QUIET table.
            if ch.get("dead_air_requires_quiet_table", True):
                end_ts = nxt["ts"] if nxt else (now if now is not None else None)
                between = [x for x in T[r["i"] + 1:]
                           if x["ts"] is not None
                           and (end_ts is None or x["ts"] < end_ts)
                           and not _is_gm(x, ch) and not _is_dice(x, ch)]
                if len(between) >= th.get("quiet_table_max_messages", 3):
                    continue
            if True:
                findings.append(_finding(
                    "R7", f"dead_air_seconds={limit}",
                    (f"GM took {int(gap)}s to respond after "
                     if nxt else f"GM silent for {int(gap)}s (ongoing) after ")
                    + f"{r['author']}'s message",
                    _excerpt(r)))

    # R8 unresolved-session-end: open R1/R2/R3 findings whose evidence sits in
    # the final quarter of the transcript.
    if "R8" in enabled and closed and findings and T:
        tail_start = len(T) * 3 // 4
        open_tail = [f for f in findings if f["rule"] in ("R1", "R2", "R3")
                     and f["evidence"].get("index", len(T)) >= tail_start]
        if open_tail:
            findings.append(_finding(
                "R8", "session end",
                f"session ends with {len(open_tail)} unresolved finding(s): "
                + ", ".join(sorted({f['rule'] for f in open_tail})),
                {"open": [f["rule"] for f in open_tail]}))

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
                        or (next_gm is None and mode == "live"
                            and now is not None)):
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

    incomplete = []
    if not normalized_transcript:
        incomplete.append(issue("transcript.empty", "/transcript",
                                "no usable transcript messages were supplied"))
    elif not any(row["content"].strip() for row in normalized_transcript):
        incomplete.append(issue(
            "transcript.no_effective_content", "/transcript",
            "the transcript contains no nonempty message content"))
    elif not any(_is_gm(row, normalized_charter)
                 for row in normalized_transcript):
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
            eligible_rules=eligible, charter=normalized_charter)

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
        charter=normalized_charter)


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
