---
name: dmcheck
version: 0.5.5
description: >
  Deterministic conduct verdicts for live tabletop sessions — CI for running
  a game. Use it when you want to know how a session went as a *table*:
  "did I leave anyone hanging?", "was that roll ever acknowledged?", "did
  the fight end without anyone being told?", "why did the table go quiet
  for five minutes?", post-session retros, or live monitoring while a
  session runs. Findings cite the table charter; ambiguity produces
  silence, never accusations.
---

# dmcheck — conduct referee for live tables

Feed it a session transcript (JSONL) plus a table charter; it returns named
findings — the unanswered question, the unacknowledged roll, the turn that
began with no cue, the event nobody narrated, the dead air. Model-free and
deterministic: same transcript, same findings, every time.

## Three things to remember

1. **A false accusation is the unforgivable bug.** A rule fires only when
   the transcript *provably* shows the violation. No findings does not mean
   a perfect session — it means nothing was provable. Never editorialize
   beyond the findings.
2. **Every finding cites the charter rule and carries the evidence.** Relay
   both; the evidence excerpt is what makes a finding fair.
3. **Name the GM.** The packaged default charter names no GM author — pass
   `--gm "<name>"` (or a charter with `gm` set) or you get exit 2, not
   findings.

## Exit codes ARE the verdict

`0` = clean (silence) · `1` = findings · `2` = unusable input/charter (the
honest lane — fix the input, don't retry blind). Failures print JSON on
stderr, never a traceback.

## Invocation

```bash
dmcheck run session.jsonl --gm "Greta"                      # quick: default charter + GM override
dmcheck run session.jsonl --charter our-table.json --ledger events.jsonl
dmcheck watch session.jsonl --follow --gm "Greta"           # live, during the session
dmcheck rules                                               # the rule set (R1–R8) with definitions
dmcheck init [--dm]                                         # starter charter (+ DM-CORE.md defaults)
dmcheck lint-charter our-table.json
```

Transcript lines: `{"author": "...", "content": "...", "ts": ...}`. Ledger
(optional): engine events to check narration against. MCP server included.

## Worked example

```bash
dmcheck run session.jsonl --charter our-table.json --ledger events.jsonl
```

```json
{
 "charter_version": "1.0",
 "messages": 9,
 "findings": [
  {
   "rule": "R2",
   "summary": "unconsumed-roll: a dice result was never followed by any GM message",
   "charter": "roll_ack_within_messages=4",
   "detail": "dice result from DiceBot never followed by a GM message",
   "evidence": {"index": 6, "author": "DiceBot", "content": "Bram rolls 1d20+4: [18] = 22"}
  },
  {
   "rule": "R3",
   "summary": "unnarrated-event: an engine event newer than the last GM message (never told to the table)",
   "charter": "ledger vs last GM message",
   "detail": "engine event never narrated: guard drops to 0 HP",
   "evidence": {"ledger_ts": 1700000800.0, "text": "guard drops to 0 HP"}
  }
 ],
 "counts": {"R2": 1, "R3": 1}
}
```

Exit 1. A clean session prints `"findings": []` and exits 0 — silence is
the pass verdict, by design.

## MUST / MUST NOT

- MUST pass `--gm` (and `--dice-bot` if a bot rolls) when using the default
  charter; author names are how every rule tells who's who.
- MUST include the evidence excerpt when relaying a finding to a human.
- MUST treat silence as "nothing provable", never as "nothing wrong" — and
  findings as "provable per charter", never as a character judgment.
- MUST NOT paraphrase findings into accusations the tool didn't make, or
  aggregate them into a score — there is deliberately no composite score.
- MUST NOT judge the narrative quality of rulings; conduct is process, not
  art.
- MUST NOT invoke a model to re-check the transcript; the verdict path is
  code, and that's the point.

## Validation checkpoints (self-audit before relaying)

GM/dice authors named · charter version noted · exit code read · evidence
attached to every relayed finding · silence reported as silence.

## Cross-skill workflows (check family)

- **Session retro:** table-kit's session JSONL is dmcheck-compatible — run
  dmcheck over it after close-out; findings (or silence) go to the humans,
  who decide any process change.
- **Live table:** `dmcheck watch --follow` alongside table-kit during play;
  `--notify-cmd` fires per open finding.
- **Rules vs conduct:** a ruling's *legality* is srdcheck's lane; whether
  the table was *told about it properly* is this tool's lane.

Family contract: [FAMILY.md](https://github.com/chaoz23/srdcheck/blob/main/FAMILY.md).

## Changelog / stale-knowledge deltas

- **0.5.5:** `init --dm` fixed for installed users (packaging omitted
  dm_core.md through 0.5.1–0.5.4); degraded errors are structured JSON.
- **post-0.5.5:** no-GM charter is a clean exit 2 with a JSON error — if you
  remember a KeyError traceback from `dmcheck run`, that's fixed.
- **0.4.x:** evidence bars tightened (R1 requires GM-directed evidence);
  `watch` + `--craft` advisory lane added. Craft events are advisory-only
  and never appear as findings.

Findings are advisory: the table's humans own every judgment about people.
