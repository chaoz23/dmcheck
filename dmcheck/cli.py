"""dmcheck CLI. Exit codes: 0 = clean, 1 = findings, 2 = unusable input/charter."""
import argparse
import json
import sys

from . import RULES, check, load_charter, load_ledger, load_transcript

SCHEMA = {
    "name": "dmcheck",
    "commands": {
        "run": "check a transcript (+ optional --ledger) against a charter; findings as JSON",
        "rules": "list the rule set with one-line definitions",
    },
    "transcript": "JSONL of {ts, author, content} OR a JSON array of Discord-API-shaped messages",
    "ledger": "optional JSONL of {ts, type: turn|act|event, actor?, text?}",
    "charter": "versioned JSON config (see charters/default.json); charter.gm is REQUIRED",
    "exit_codes": {"0": "clean", "1": "findings present", "2": "charter or input unusable"},
}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="dmcheck", description=__doc__)
    ap.add_argument("command", nargs="?", choices=["run", "rules"], default="run")
    ap.add_argument("transcript", nargs="?")
    ap.add_argument("--charter", help="charter JSON (default: packaged default)")
    ap.add_argument("--ledger", help="optional engine-event ledger JSONL")
    ap.add_argument("--gm", action="append", help="GM author name (repeatable; overrides charter.gm)")
    ap.add_argument("--schema", action="store_true")
    ap.add_argument("--version", action="version", version="dmcheck 0.1.0")
    a = ap.parse_args(argv)

    if a.schema:
        print(json.dumps(SCHEMA, indent=1))
        return 0
    if a.command == "rules":
        print(json.dumps(RULES, indent=1))
        return 0
    if not a.transcript:
        ap.error("a transcript file is required")
    try:
        ch = load_charter(a.charter)
        if a.gm:
            ch["gm"] = a.gm
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
