"""dmcheck CLI. Exit codes: 0 = clean, 1 = findings, 2 = unusable input/charter."""

ORIGINS = {
    "R1": "a player asked the DM a lore question; another PLAYER ended up answering",
    "R2": "'did I hit?' — a successful attack roll sat unacknowledged at a live table",
    "R3": "the state engine resolved a hit the table never heard about",
    "R4": "'isn't it her turn?' — asked by a player, which is one player too many",
    "R5": "the engine rejected an out-of-turn action silently; the table never knew",
    "R6": "a module's hidden state names leaked into narration",
    "R7": "five minutes of GM silence reads as absence, not thinking",
    "R8": "sessions should end in a defined state — that's what makes the next one possible",
}
import argparse
import json
import sys

from . import RULES, check, load_charter, load_ledger, load_transcript

SCHEMA = {
    "name": "dmcheck",
    "commands": {
        "run": "check a transcript (+ optional --ledger) against a charter; findings as JSON",
        "rules": "list the rule set with one-line definitions",
        "charter": "print the effective charter (defaults or --charter file) — copy, edit, version it",
        "init": "bootstrap a new table: write a starter charter + print the session-zero checklist (S1-S8)",
        "watch": "live mode: incremental checking over a growing transcript (tail or stdin JSONL)",
        "lint-charter": "validate a charter file (unknown keys, bad thresholds, seat config)",
        "explain": "one rule in depth: definition, charter knobs, origin story",
    },
    "transcript": "JSONL of {ts, author, content} OR a JSON array of Discord-API-shaped messages",
    "ledger": "optional JSONL of {ts, type: turn|act|event, actor?, text?}",
    "charter": "versioned JSON config (see charters/default.json); charter.gm is REQUIRED",
    "exit_codes": {"0": "clean", "1": "findings present", "2": "charter or input unusable"},
}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="dmcheck", description=__doc__)
    ap.add_argument("command", nargs="?", choices=["run", "rules", "charter", "init", "watch", "lint-charter", "explain"], default="run")
    ap.add_argument("transcript", nargs="?")
    ap.add_argument("--charter", help="charter JSON (default: packaged default)")
    ap.add_argument("--ledger", help="optional engine-event ledger JSONL")
    ap.add_argument("--gm", action="append", help="GM author name (repeatable; OVERRIDES charter.gm)")
    ap.add_argument("--dice-bot", action="append", help="dice-bot author name (repeatable; overrides charter.dice_authors)")
    ap.add_argument("--schema", action="store_true")
    ap.add_argument("--follow", action="store_true", help="watch: keep tailing the file")
    ap.add_argument("--interval", type=float, default=5.0, help="watch: tick seconds")
    ap.add_argument("--notify-cmd", help="watch: shell command fired per OPEN finding (finding JSON on stdin)")
    ap.add_argument("--version", action="version", version="dmcheck 0.4.0")
    a = ap.parse_args(argv)

    if a.schema:
        print(json.dumps(SCHEMA, indent=1))
        return 0
    if a.command == "rules":
        print(json.dumps(RULES, indent=1))
        return 0
    if a.command == "charter":
        print(json.dumps(load_charter(a.charter), indent=1))
        return 0
    if a.command == "init":
        from datetime import date
        out = a.transcript or "charter.json"
        charter = {
            "charter_version": "1.0",
            "effective_date": date.today().isoformat(),
            "description": "Table charter — adopted at session zero; edit and bump charter_version on change.",
            "gm": a.gm or ["YOUR-GM-AUTHOR-NAME"],
            "players": [],
            "dice_authors": a.dice_bot or ["Dice Maiden"],
            "ooc_markers": ["[OOC]", "(("],
            "hidden_terms": [],
            "thresholds": {"answer_within_messages": 6, "roll_ack_within_messages": 4,
                           "dead_air_seconds": 300, "cue_within_gm_messages": 3},
            "rules_enabled": sorted(RULES),
            "seats": {},
            "question_requires_gm_address": True,
            "dead_air_requires_quiet_table": True,
            "_notes": {
                "gm": "author name(s) as they appear in the transcript — REQUIRED",
                "seats": "per-seat delivery policy incl. aliases (in-fiction cues are by CHARACTER name ~10:1 at pro tables): {\"Ash\": {\"aliases\": [\"Shalia\"], ...}}; also: {name: {cue_requires_mention: true, mention: '<@id>'}} for agent seats behind mention-gated transports",
                "hidden_terms": "module spoiler names — R6 fires if they leak into GM messages",
                "thresholds": "R1/R2/R7/R4 knobs; every finding cites the value it judged against",
            },
        }
        with open(out, "w") as f:
            json.dump(charter, f, indent=1)
        print(json.dumps({
            "charter_written": out,
            "edit_first": ["gm (author names)", "players", "hidden_terms",
                           "seats for any mention-gated agent seat"],
            "session_zero": {
                "S1": "contracts go where each agent actually reads (its own memory/config, not just chat)",
                "S2": "verify every transport end-to-end, BOTH directions (one native post from each seat)",
                "S3": "declare the table's rules boundary first (e.g. SRD 5.2.1) — the GM's move",
                "S3b": "compile every character against the declared boundary; bank rulings for out-of-boundary features",
                "S3c": "bootstrap sheet accountability: sheet upkeep is the PLAYER'S obligation, declared out loud; settlement quizzes are graded silently",
                "S4": "tooling handshake out loud in-channel: who rolls what, which ledger records, where verdicts come from",
                "S5": "lane contracts: GM = rules chair, world-truth, turn order — exclusively; players play, no second-GMing",
                "S6": "tempo contract: detection latency, beat pace, holding rules",
                "S7": "story lane if running a module (spine/guard init before play)",
                "S8": "probe the referee once — a clean opening check PASS proves the guard is actually watching",
            },
            "ledger_format": "JSONL {ts, type: turn|act|event, actor, text} — feed via --ledger; same format dmcheck watch consumes",
            "next": [f"dmcheck lint-charter {out}",
                     f"dmcheck watch <transcript> --charter {out} --follow"],
        }, indent=1))
        return 0
    if a.command == "explain":
        rid = (a.transcript or "").upper()
        if rid not in RULES:
            print(json.dumps({"error": f"unknown rule {rid!r}; try R1..R8"}), file=sys.stderr)
            return 2
        ch = load_charter(a.charter)
        print(json.dumps({"rule": rid, "definition": RULES[rid],
                          "charter_knobs": ch.get("thresholds", {}),
                          "origin": ORIGINS.get(rid, "")}, indent=1))
        return 0
    if a.command == "lint-charter":
        try:
            ch = json.load(open(a.transcript or a.charter))
        except (OSError, json.JSONDecodeError) as e:
            print(json.dumps({"error": str(e)}), file=sys.stderr)
            return 2
        KNOWN = {"charter_version", "effective_date", "description", "gm", "players",
                 "dice_authors", "ooc_markers", "hidden_terms", "thresholds",
                 "rules_enabled", "seats", "question_requires_gm_address",
                 "dead_air_requires_quiet_table"}
        KNOWN_TH = {"answer_within_messages", "roll_ack_within_messages",
                    "dead_air_seconds", "cue_within_gm_messages",
                    "quiet_table_max_messages"}
        problems = [f"unknown key '{k}'" for k in ch
                    if k not in KNOWN and not k.startswith("_")]
        problems += [f"unknown threshold '{k}'" for k in ch.get("thresholds", {})
                     if k not in KNOWN_TH]
        problems += [f"threshold '{k}' must be a positive number"
                     for k, v in ch.get("thresholds", {}).items()
                     if not isinstance(v, (int, float)) or v <= 0]
        for name, sc in (ch.get("seats") or {}).items():
            if not isinstance(sc, dict):
                problems.append(f"seat '{name}' must be an object")
                continue
            problems += [f"seat '{name}': unknown key '{k}'"
                         for k in set(sc) - {"cue_requires_mention", "mention",
                                             "aliases"}]
            if sc.get("cue_requires_mention") and not sc.get("mention"):
                problems.append(f"seat '{name}': cue_requires_mention without a "
                                f"mention string — unverifiable; R4 falls back to name matching")
        if not ch.get("gm"):
            problems.append("charter.gm is empty — dmcheck cannot referee without it")
        print(json.dumps({"problems": problems, "ok": not problems}, indent=1))
        return 1 if problems else 0
    if a.command == "watch":
        from .watch import watch_main
        if not a.transcript:
            ap.error("watch requires a transcript file or '-' for stdin")
        return watch_main(a)
    if not a.transcript:
        ap.error("a transcript file is required")
    try:
        ch = load_charter(a.charter)
        if a.gm:
            ch["gm"] = a.gm
        if a.dice_bot:
            ch["dice_authors"] = a.dice_bot
        transcript = load_transcript(a.transcript)
        ledger = load_ledger(a.ledger)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        return 2
    findings, code = check(transcript, ch, ledger)
    print(json.dumps({"charter_version": ch.get("charter_version"),
                      "messages": len(transcript),
                      "findings": findings,
                      "counts": {r: sum(1 for f in findings if f["rule"] == r)
                                 for r in sorted({f["rule"] for f in findings})}},
                     indent=1))
    return code


if __name__ == "__main__":
    sys.exit(main())
