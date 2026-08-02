"""dmcheck CLI. Exit codes: 0 = clean, 1 = findings, 2 = unusable input/charter."""

ORIGINS = {
    "R1": "a player asked the DM a lore question; another PLAYER ended up answering",
    "R2": "'did I hit?' — a successful attack roll sat unacknowledged at a live table",
    "R3": "the state engine resolved a hit the table never heard about",
    "R4": "'isn't it her turn?' — asked by a player, which is one player too many",
    "R5": "retired after actor != turn owner falsely accused legal reactions and interrupts",
    "R6": "a module's hidden state names leaked into narration",
    "R7": "five minutes of GM silence reads as absence, not thinking",
    "R8": "sessions should end in a defined state — that's what makes the next one possible",
}
import argparse
import os
import json
import math
import sys

from . import (RULES, __version__, evaluate_paths, evaluate_table_event_path,
               load_charter)
from .core import (invalid_result, public_charter, public_charter_digest,
                   redact_output)
from .validation import (InputValidationError, issue, load_craft_input,
                         normalize_charter)

SCHEMA = {
    "name": "dmcheck",
    "commands": {
        "run": "check a transcript (+ optional --ledger) against a charter; findings as JSON",
        "run-events": "check a table.event/1.0 JSONL stream; gaps and unsupported events fail closed",
        "rules": "list the rule set with one-line definitions",
        "charter": "print the effective charter with hidden values redacted; edit the source file to change protected terms",
        "craft": "attention lane: rate statistics vs the professional envelope, ONE attention signal, categorical defects (voiced-PC etc.) - advisory, never a score",
        "init": "bootstrap a new table: write a starter charter + print the session-zero checklist (S1-S8)",
        "watch": "live mode: incremental checking over a growing transcript (tail or stdin JSONL)",
        "lint-charter": "validate a charter file (unknown keys, bad thresholds, seat config)",
        "explain": "one rule in depth: definition, charter knobs, origin story",
    },
    "transcript": "UTF-8 JSONL of {ts, author, content, id?, audience?, reply_to?, correlation_id?, roll_id?} OR a JSON array of Discord-API-shaped messages",
    "table_event": "run-events accepts table.event/1.0 JSONL or a JSON array; contract gaps and unsupported variants return incomplete",
    "ledger": "optional JSONL of {ts, type: turn|act|event, id?, actor?, text?}; event IDs correlate narration; actor/turn order never establishes action legality",
    "charter": "schema-versioned JSON config; canonical packaged default is dmcheck/default_charter.json; charter.gm is REQUIRED",
    "authority": "dmcheck evaluates conduct/communication only; upstream authority and the DM/table decide action legality; srdcheck is advisory",
    "findings": "each finding has a deterministic finding_id; mutable detail never changes lifecycle identity",
    "terminal": "session end reports typed evaluation state plus incomplete obligations and source coverage",
    "evaluation_ts": "optional Unix-seconds horizon for exact replay of time-based findings",
    "exit_codes": {"0": "clean", "1": "findings present", "2": "charter or input unusable"},
    "result": {"schema_version": "1.0",
               "evaluation_commands": ["run", "run-events", "watch", "mcp.run",
                                       "direct.evaluate", "direct.evaluate_table_event_path"],
               "status": ["clean", "findings", "invalid", "incomplete"],
               "craft_status": ["advisory", "invalid", "incomplete"],
               "schemas": ["charter.schema.json", "transcript.schema.json",
                           "ledger.schema.json", "evaluation-result.schema.json"]},
    "mcp": {
        "release_status": "unreleased %s candidate" % __version__,
        "protocol": "2026-07-28",
        "transport": "stdio",
        "lifecycle": "stateless batch only",
        "tools": ["run", "rules"],
        "path_policy": (
            "denied unless explicitly allowlisted and secure component-relative "
            "no-follow opens are available"),
        "text_content": (
            "security-redacted projection; canonical structuredContent remains "
            "untrusted quoted data"),
        "tool_rate_limit": "120/minute, burst 32, process-local",
        "not_exposed": ["watch", "craft", "tasks", "resources", "prompts"],
    },
}


def _print_invalid(problems, mode="closed"):
    result = invalid_result(problems, mode=mode)
    print(json.dumps(result.to_dict(), indent=1))
    return result.exit_code


def main(argv=None):
    ap = argparse.ArgumentParser(prog="dmcheck", description=__doc__)
    ap.add_argument("command", nargs="?", choices=["run", "run-events", "rules", "charter", "init", "watch", "lint-charter", "explain", "craft"], default="run")
    ap.add_argument("transcript", nargs="?")
    ap.add_argument("--charter", help="charter JSON (default: packaged default)")
    ap.add_argument("--ledger", help="optional engine-event ledger JSONL")
    ap.add_argument("--gm", action="append", help="GM author name (repeatable; OVERRIDES charter.gm)")
    ap.add_argument("--dice-bot", action="append", help="dice-bot author name (repeatable; overrides charter.dice_authors)")
    ap.add_argument("--schema", action="store_true")
    ap.add_argument("--follow", action="store_true", help="watch: keep tailing the file")
    ap.add_argument("--interval", type=float, default=5.0, help="watch: tick seconds")
    ap.add_argument("--craft", action="store_true", help="watch: also emit advisory craft_attention/craft_defect events")
    ap.add_argument("--dm", action="store_true", help="init: also write DM-CORE.md (the nine defaults + calibration pointer)")
    ap.add_argument("--scene", default="SOCIAL", help="craft: COMBAT|SOCIAL|EXPLORATION")
    ap.add_argument("--pc", action="append", help="craft: PC name (repeatable) for voiced-PC detection")
    ap.add_argument("--notify-cmd", help="watch: shell command fired per source-observed OPEN finding (finding JSON on stdin)")
    ap.add_argument("--evaluation-ts", type=float,
                    help="run: explicit Unix-seconds evaluation horizon")
    ap.add_argument("--version", action="version",
                    version="dmcheck %s" % __version__)
    a = ap.parse_args(argv)

    if a.schema:
        print(json.dumps(SCHEMA, indent=1))
        return 0
    if a.command == "rules":
        print(json.dumps(RULES, indent=1))
        return 0
    if a.command == "charter":
        try:
            charter = load_charter(a.charter, gm=a.gm,
                                   dice_authors=a.dice_bot)
        except InputValidationError as exc:
            return _print_invalid(exc.issues)
        print(json.dumps(public_charter(charter), indent=1))
        return 0
    if a.command == "craft":
        from .craft import evaluate as evaluate_craft, failure as craft_failure
        if not a.transcript:
            result = craft_failure([
                issue("input.path_required", "/craft",
                      "craft requires a beats or transcript path")
            ])
            print(json.dumps(result, indent=1))
            return result["exit_code"]
        try:
            charter = load_charter(a.charter, gm=a.gm)
            gm_authors = charter.get("gm") or ["GM"]
            raw = load_craft_input(a.transcript)
        except InputValidationError as exc:
            result = craft_failure(exc.issues)
            print(json.dumps(result, indent=1))
            return result["exit_code"]
        result = evaluate_craft(raw, a.scene, tuple(a.pc or []), gm_authors)
        print(json.dumps(redact_output(result, charter), indent=1))
        return result["exit_code"]
    if a.command == "init":
        from datetime import date
        out = a.transcript or "charter.json"
        try:
            charter = load_charter()
            charter.pop("charter_digest", None)
            charter.update({
                "effective_date": date.today().isoformat(),
                "description": "Table charter — adopted at session zero; edit and bump charter_version on change.",
                "gm": a.gm or ["YOUR-GM-AUTHOR-NAME"],
                "dice_authors": a.dice_bot or charter["dice_authors"],
                "_notes": {
                    "gm": "author name(s) as they appear in the transcript — REQUIRED",
                    "seats": "per-seat delivery policy incl. aliases (in-fiction cues are by CHARACTER name ~10:1 at pro tables): {\"Ash\": {\"aliases\": [\"Shalia\"], ...}}; also: {name: {cue_requires_mention: true, mention: '<@id>'}} for agent seats behind mention-gated transports",
                    "hidden_terms": "module spoiler names — R6 fires if they leak into GM messages",
                    "thresholds": "R1/R2/R7/R4 knobs; every finding cites the value it judged against",
                },
            })
            charter = normalize_charter(charter, require_gm=True)
            with open(out, "w", encoding="utf-8") as handle:
                json.dump(charter, handle, indent=1, ensure_ascii=False)
                handle.write("\n")
        except InputValidationError as exc:
            return _print_invalid(exc.issues)
        except (OSError, UnicodeError) as exc:
            return _print_invalid([
                issue("input.unwritable", "/charter",
                      "charter could not be written: %s" % exc)
            ])
        if a.dm:
            core = os.path.join(os.path.dirname(__file__), "dm_core.md")
            if not os.path.exists(core):
                # Packaging defect, not user error. Say so plainly instead of
                # raising a traceback at someone setting up a table.
                print(json.dumps({
                    "error": "packaging-incomplete",
                    "detail": f"dm_core.md is missing from this install ({core}). "
                              "The charter was written; DM-CORE.md was not. "
                              "Reinstall dmcheck>=0.5.5, or fetch DM-CORE.md from "
                              "https://github.com/chaoz23/dmcheck",
                    "charter_written": out,
                }, indent=1))
                return 2
            try:
                with open(core, encoding="utf-8") as source:
                    core_text = source.read()
                with open("DM-CORE.md", "w", encoding="utf-8") as target:
                    target.write(core_text)
            except (OSError, UnicodeError) as exc:
                return _print_invalid([
                    issue("input.unwritable", "/dm_core",
                          "DM-CORE.md could not be written: %s" % exc)
                ])
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
            "ledger_format": "JSONL {ts, type: turn|act|event, id, actor, text}; narration carries correlation_id — feed via --ledger; same format dmcheck watch consumes",
            "next": [f"dmcheck lint-charter {out}",
                     f"dmcheck watch <transcript> --charter {out} --follow"],
        }, indent=1))
        return 0
    if a.command == "explain":
        rid = (a.transcript or "").upper()
        if rid not in RULES:
            return _print_invalid([
                issue("charter.unknown_rule", "/rule",
                      f"unknown rule {rid!r}; try R1..R8")
            ])
        try:
            ch = load_charter(a.charter)
        except InputValidationError as exc:
            return _print_invalid(exc.issues)
        output = {"rule": rid, "definition": RULES[rid],
                  "charter_knobs": ch.get("thresholds", {}),
                  "origin": ORIGINS.get(rid, "")}
        print(json.dumps(redact_output(output, ch), indent=1))
        return 0
    if a.command == "lint-charter":
        path = a.transcript or a.charter
        if not path:
            return _print_invalid([
                issue("input.path_required", "/charter",
                      "lint-charter requires a charter path")
            ])
        raw_charter = None
        try:
            with open(path, encoding="utf-8") as handle:
                raw_charter = json.load(handle)
            ch = normalize_charter(raw_charter, require_gm=True)
        except InputValidationError as exc:
            result = invalid_result(exc.issues)
            output = redact_output(result.to_dict(),
                                   raw_charter if isinstance(raw_charter, dict)
                                   else {})
            print(json.dumps(output, indent=1))
            return result.exit_code
        except (OSError, UnicodeError, json.JSONDecodeError):
            # Reuse the canonical loader's fail-closed diagnostics for file
            # and JSON failures where no safely parsed redaction policy exists.
            try:
                load_charter(path)
            except InputValidationError as exc:
                return _print_invalid(exc.issues)
            raise
        print(json.dumps({
            "status": "clean", "exit_code": 0, "ok": True, "problems": [],
            "schema_version": ch["schema_version"],
            "charter_version": ch["charter_version"],
            "charter_digest": public_charter_digest(ch),
            "charter_digest_scope": "public-policy; hidden values withheld",
        }, indent=1))
        return 0
    if a.command == "watch":
        from .watch import watch_main
        if not a.transcript:
            return _print_invalid([
                issue("input.path_required", "/transcript",
                      "watch requires a transcript path or '-' for stdin")
            ], mode="live")
        if not math.isfinite(a.interval) or a.interval <= 0:
            return _print_invalid([
                issue("watch.interval", "/interval",
                      "watch interval must be finite and greater than zero")
            ], mode="live")
        return watch_main(a)
    if not a.transcript:
        return _print_invalid([
            issue("input.path_required", "/transcript",
                  "%s requires an input path" % a.command)
        ])
    if a.command == "run-events":
        if a.ledger is not None:
            return _print_invalid([
                issue("table_event.ledger_conflict", "/ledger",
                      "run-events derives its ledger from the event stream; --ledger is not accepted")
            ])
        result = evaluate_table_event_path(
            a.transcript, charter_path=a.charter, gm=a.gm,
            dice_authors=a.dice_bot, mode="closed", now=a.evaluation_ts)
    else:
        result = evaluate_paths(
            a.transcript, charter_path=a.charter, ledger_path=a.ledger,
            gm=a.gm, dice_authors=a.dice_bot, mode="closed",
            now=a.evaluation_ts)
    print(json.dumps(result.to_dict(), indent=1))
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
