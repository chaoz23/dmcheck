---
name: dmcheck
version: 0.6.0
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
dmcheck run-events events.jsonl --gm "Greta"                # table.event/1.0 streams (fail closed)
dmcheck watch session.jsonl --follow --gm "Greta"           # live, during the session
dmcheck rules                                               # the rule set with definitions (R5 retired)
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
 "result_schema_version": "1.0",
 "status": "findings",
 "exit_code": 1,
 "messages": 9,
 "findings": [
  {
   "finding_id": "r2-74e366c6f414e0dbb5cd5ffa",
   "rule": "R2",
   "summary": "unconsumed-roll: a dice result got no correlated GM narration within threshold",
   "charter": "roll_ack_within_messages=4; correlation=explicit",
   "detail": "possible dice result from DiceBot has no provably correlated GM narration within 4 messages; text-only or correlation evidence is incomplete",
   "evidence": {"index": 6, "ts": 1700000700.0, "author": "DiceBot",
                "content": "Bram rolls 1d20+4: [18] = 22"},
   "severity": "advisory",
   "provenance": "inferred",
   "confidence": "low",
   "charter_version": "1.0",
   "charter_digest": "sha256:9882b2f4..."
  }
 ],
 "counts": {"R2": 1}
}
```

Exit 1. **Read `severity` and `provenance` before relaying:** only
`severity: "finding"` + `provenance: "observed"` is a provable violation;
`advisory`/`inferred`/`confidence: "low"` is a lead, not an accusation. A
clean session prints `"findings": []`, `"status": "clean"`, exit 0 —
silence is the pass verdict, by design.

## MUST / MUST NOT

- MUST pass `--gm` (and `--dice-bot` if a bot rolls) when using the default
  charter; author names are how every rule tells who's who.
- MUST include the evidence excerpt when relaying a finding to a human.
- MUST treat silence as "nothing provable", never as "nothing wrong" — and
  findings as "provable per charter", never as a character judgment.
- MUST NOT paraphrase findings into accusations the tool didn't make, or
  aggregate them into a score — there is deliberately no composite score.
- MUST NOT present an `advisory`/`inferred` finding as a violation; the
  no-false-accusation contract lives in those two fields.
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

- **0.6.0:** result envelope is `result_schema_version: "1.0"` with
  `status` (clean|findings|invalid|incomplete); findings carry
  `finding_id`, `severity` (finding|advisory), `provenance`
  (observed|inferred), `confidence`, and a sha256 `charter_digest`
  (public-policy scope — hidden spoiler values withheld). **R5 retired**
  (actor≠turn-owner is legal: reactions, readied actions, legendary
  actions). `run-events` ingests table.event/1.0 streams, fail closed.
  Invalid/incomplete input is a structured exit-2 envelope, never a
  traceback. If you remember flat findings or 0.5.x output, that's stale.
- **0.5.5:** `init --dm` fixed for installed users (packaging omitted
  dm_core.md through 0.5.1–0.5.4).

Findings are advisory: the table's humans own every judgment about people.
