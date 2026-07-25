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
"""

import json
import os
from datetime import datetime, timezone

RULES = {
    "R1": "unanswered-player: a player question to the table got no GM response within threshold",
    "R2": "unconsumed-roll: a dice result was never followed by any GM message",
    "R3": "unnarrated-event: an engine event newer than the last GM message (never told to the table)",
    "R4": "missing-cue: a turn began and the next GM messages never addressed the actor by name",
    "R5": "out-of-initiative: a ledger action by an actor whose turn it was not",
    "R6": "spoiler-leak: a configured hidden term appeared in a GM message",
    "R7": "dead-air: GM silence beyond threshold while a player waited",
    "R8": "unresolved-session-end: the transcript ends with open R1/R2/R3 findings in its tail",
}


# ---------- loading ----------

def load_charter(path=None):
    default = os.path.join(os.path.dirname(__file__), "..", "charters", "default.json")
    packaged = os.path.join(os.path.dirname(__file__), "default_charter.json")
    src = path or (default if os.path.exists(default) else packaged)
    with open(src) as f:
        ch = json.load(f)
    ch.setdefault("thresholds", {})
    ch.setdefault("gm", [])
    ch.setdefault("dice_authors", [])
    ch.setdefault("ooc_markers", [])
    ch.setdefault("hidden_terms", [])
    ch.setdefault("rules_enabled", list(RULES))
    return ch


def _parse_ts(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def load_transcript(path):
    """Accepts: JSONL of {ts, author, content} — or a JSON array (Discord-API
    message shape: {timestamp, author:{username}, content}) in either order."""
    with open(path) as f:
        text = f.read().strip()
    rows = []
    if text.startswith("["):
        raw = json.loads(text)
        for m in raw:
            author = m.get("author")
            if isinstance(author, dict):
                author = author.get("username")
            rows.append({"ts": _parse_ts(m.get("ts") or m.get("timestamp")),
                         "author": author, "content": m.get("content") or ""})
    else:
        for line in text.splitlines():
            if not line.strip():
                continue
            m = json.loads(line)
            rows.append({"ts": _parse_ts(m.get("ts")), "author": m.get("author"),
                         "content": m.get("content") or ""})
    rows = [r for r in rows if r["author"] is not None]
    if len(rows) > 1 and rows[0]["ts"] and rows[-1]["ts"] and rows[0]["ts"] > rows[-1]["ts"]:
        rows.reverse()  # newest-first exports → chronological
    for i, r in enumerate(rows):
        r["i"] = i
    return rows


def load_ledger(path):
    """Optional engine ledger: JSONL of {ts?, type, actor?, text?}.
    Recognized types: turn (actor's turn began), act (actor did a thing),
    event (anything narratable)."""
    if not path:
        return []
    rows = []
    with open(path) as f:
        for line in f:
            if line.strip():
                e = json.loads(line)
                e["ts"] = _parse_ts(e.get("ts"))
                rows.append(e)
    return rows


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


def _word_in(term, text):
    import re
    return re.search(r"(?<![A-Za-z0-9_])" + re.escape(term) + r"(?![A-Za-z0-9_])",
                     text, re.I) is not None


# ---------- the rules ----------

def check(transcript, charter, ledger=None, closed=True, now=None):
    ch = charter
    ledger = ledger or []
    T = transcript
    th = ch["thresholds"]
    findings = []
    enabled = set(ch.get("rules_enabled", list(RULES)))
    gm_idx = [r["i"] for r in T if _is_gm(r, ch)]
    if not ch["gm"]:
        return [{"error": "charter names no GM author — cannot referee; "
                          "set charter.gm or pass --gm"}], 2

    # R1 unanswered-player: a non-GM, non-dice message containing a question,
    # with NO GM message in the next `answer_within_messages` messages.
    if "R1" in enabled:
        n = th.get("answer_within_messages", 6)
        for r in T:
            if _is_gm(r, ch) or _is_dice(r, ch) or "?" not in r["content"]:
                continue
            window = [x for x in T[r["i"] + 1: r["i"] + 1 + n]]
            complete = len(window) >= n or closed
            if window and complete and not any(_is_gm(x, ch) for x in window):
                findings.append(_finding(
                    "R1", f"answer_within_messages={n}",
                    f"question from {r['author']} got no GM response within {n} messages",
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
                if e.get("type") in ("event", "act") and e.get("ts") \
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
            gms_after = [r for r in T if _is_gm(r, ch) and r["ts"] and r["ts"] >= e["ts"]][:k]
            if gms_after and not any(_word_in(e["actor"], r["content"]) for r in gms_after):
                findings.append(_finding(
                    "R4", f"cue_within_gm_messages={k}",
                    f"turn began for {e['actor']} but the next {len(gms_after)} GM "
                    f"message(s) never address them by name",
                    {"actor": e["actor"], "ledger_ts": e["ts"]}))

    # R5 out-of-initiative: ledger `act` whose actor differs from the actor of
    # the most recent `turn` event.
    if "R5" in enabled and ledger:
        current = None
        for e in ledger:
            if e.get("type") == "turn":
                current = e.get("actor")
            elif e.get("type") == "act" and current and e.get("actor") \
                    and e["actor"] != current:
                findings.append(_finding(
                    "R5", "ledger turn order",
                    f"{e['actor']} acted during {current}'s turn",
                    {"actor": e["actor"], "turn_of": current,
                     "text": (e.get("text") or "")[:140]}))

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
            nxt = next((x for x in T[r["i"] + 1:] if _is_gm(x, ch) and x["ts"]), None)
            gap = (nxt["ts"] - r["ts"]) if nxt else \
                  ((now - r["ts"]) if (now is not None and not closed) else None)
            if gap is not None and gap > limit:
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
