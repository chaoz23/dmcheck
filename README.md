# dmcheck

**Deterministic conduct verdicts for live tabletop sessions — CI for running a game.**

Feed it a session transcript (and optionally an engine event ledger) plus a **table charter**, and it returns named findings — the player whose question was never answered, the dice roll nobody acknowledged, the turn that began without anyone being told, the spoiler that leaked into the channel, the five-minute dead air. Every finding cites the charter rule it violates, with the evidence attached.

> **Cold-boot probe (2026-07-24):** a fresh agent session given only this repo URL installed and refereed a session in **2 commands**, verified all three exit-code legs against the docs, and confirmed the no-false-accusation contract held (a consumed roll produced silence). Its friction notes shipped as 0.1.1 (`--dice-bot`, `dmcheck charter`).
>
> **The design contract:** a false accusation is the unforgivable bug. A rule fires only when the transcript *provably* shows the violation — ambiguity produces silence, never noise. The verdict path is model-free and deterministic: same transcript, same findings, every time.

## 30 seconds to a refereed session

```console
$ pip install dmcheck          # stdlib only, no dependencies
$ dmcheck run session.jsonl --gm "Greta"
{
 "messages": 9,
 "findings": [
  {"rule": "R2", "summary": "unconsumed-roll: a dice result was never followed by any GM message",
   "charter": "roll_ack_within_messages=4",
   "detail": "dice result from DiceBot never followed by a GM message",
   "evidence": {"index": 6, "author": "DiceBot", "content": "Bram rolls 1d20+4: [18] = 22"}},
  ...
 ],
 "counts": {"R1": 1, "R2": 1, "R6": 1, "R7": 1, "R8": 1}
}
```

Transcript formats: JSONL of `{ts, author, content}`, or a JSON array of Discord-API-shaped messages (`{timestamp, author: {username}, content}`) in either order.

## The rule set (each one paid for by a real table failure)

| Rule | Fires when | Origin story |
|---|---|---|
| R1 | a player's question got no GM response within threshold | a player asked the DM a lore question; another *player* ended up answering |
| R2 | a dice result was never followed by any GM message | "did I hit?" — a player's successful attack roll sat unacknowledged |
| R3 | an engine event was never narrated to the table | the state engine resolved a hit the table never heard about |
| R4 | a turn began and the GM never addressed the actor by name | "isn't it her turn?" — asked by a player, which is one player too many |
| R5 | someone acted out of initiative (needs the ledger) | engine rejected it silently; the table never knew |
| R6 | a configured hidden term appeared in a GM message | a module's secret state names leaked into narration |
| R7 | GM dead air beyond threshold while a player waited | 30 seconds reads as thinking; five minutes reads as absence |
| R8 | the session ended with open R1–R3 findings in its tail | sessions should end in a defined state — that's what makes the next one possible |

These came from running a hybrid table — human and AI players, an AI GM — on Discord, where every one of these failures actually happened and got codified the same week. They apply equally to human GMs: run dmcheck over your own exported game log and see what your table's transcript says.

## Live mode (0.2): the referee sits AT the table

`dmcheck watch` runs the same engine over a *growing* session — stdin JSONL or a
tailed file — and emits lifecycle events: **OPEN** when a violation becomes
provable (thresholds fully elapsed; no predictions, ever), **RESOLVED** when a
living condition heals (the engine event finally got narrated). `--notify-cmd`
fires your own hook per OPEN finding — dmcheck itself never posts anywhere.
At session end a closed-mode pass runs, so watch's final state provably equals
`dmcheck run` on the full transcript (tested).

```console
$ your-chat-fetcher | dmcheck watch - --gm "Rob" --notify-cmd 'notify-dm.sh'
{"event": "open", "rule": "R2", "detail": "dice result from RollBot never followed by a GM message", ...}
{"event": "resolved", "rule": "R3", ...}
{"event": "session_end", "open": ["R1"], "open_count": 1}
$ dmcheck explain R2            # the rule, its charter knobs, and the table failure that earned it
$ dmcheck lint-charter my.json  # unknown keys / bad thresholds refuse loudly
```

The point of live: every failure the rules encode was recoverable in the
moment it happened — the unanswered question, the stale roll, the missing cue
all had a seconds-wide window where a nudge saved the beat. Post-hoc tells you
what went wrong last night; watch taps the GM's shoulder before the player
feels it.

## The charter is config, not code

`charters/default.json` ships thresholds and conventions derived from a real table's protocol. Override any of it — cue conventions, dead-air tolerance, dice-bot names, hidden-term lists — and version it. A league or organized-play program could publish a charter the way they publish a player's guide; dmcheck then referees any table against it.

```console
$ dmcheck run session.jsonl --charter our-table.json --ledger events.jsonl
$ dmcheck rules            # the rule set with definitions
$ dmcheck charter          # print the effective charter — copy, edit, version it
$ dmcheck run session.jsonl --gm "Rob" --dice-bot "RollBot"   # quick overrides, no file needed
$ dmcheck --schema         # machine-readable I/O contract
```

## For agents

- `tool.json` at the repo root; `--schema`; exit codes: `0` clean · `1` findings · `2` charter/input unusable.
- MCP server: `dmcheck-mcp` (stdio) with tools `run` and `rules`.
- Findings are structured JSON with rule id, charter citation, human-readable detail, and an evidence span — built to be consumed by a GM agent that fixes its own procedure between beats.

## What it does NOT do (on purpose)

- **No rules adjudication** — whether the attack was *legal* is [srdcheck](https://github.com/chaoz23/srdcheck)'s job.
- **No character math** — that's [charactercheck](https://github.com/chaoz23/charactercheck). (srdcheck judges the rules, charactercheck derives the actor, dmcheck referees the table.)
- **No narrative-quality judging** — whether the prose was *good* is taste, and taste is not checkable. dmcheck checks procedure only.
- **No model calls, no scores** — deterministic findings per rule, never a blended "DM grade."

## Credits

The rule set was distilled from live hybrid (human + AI) table sessions; the Router+Detector pattern in [native-gaming-harness](https://github.com/TinkerChen01/native-gaming-harness) independently converged on the same idea, which we take as evidence it's the load-bearing piece. dmcheck is game-system-agnostic and unaffiliated with any publisher.

<!-- MCP registry ownership marker (do not remove): binds this repo's PyPI package to its registry namespace. -->
mcp-name: io.github.chaoz23/dmcheck


## Bootstrap a new table (v0.3)

```
dmcheck init charter.json --gm YOUR-NAME
```

Writes a starter charter (versioned, effective-dated, lint-clean by
construction) and prints the session-zero checklist S1–S8 — including S3c:
sheet accountability is declared out loud at session zero, then settlement
quizzes are graded silently. The referee that judges your table also hands
you its constitution.

**Per-seat cue policy (R4, hardened).** Agent seats behind mention-gated
transports (e.g. Discord `allowBots="mentions"`) never receive name-in-prose
cues. Declare it:

```json
"seats": {"Shalia": {"cue_requires_mention": true, "mention": "<@1493...>"}}
```

R4 then counts a cue **only** if the literal mention string is present.
Origin: a live session where "Shalia — you're up" was posted, looked like a
cue, and was provably undeliverable — R4 passed on it. Never again.

**Ledger format (the declared standard).** dmcheck's ledger is JSONL:
`{ts, type: turn|act|event, actor, text}` — one line per engine event. No
lightweight OSS session-ledger existed when we surveyed (2026-07-26), so
this format is the interchange standard our stack shares: engines tap their
logs into it; `run`, `watch`, and settlement all consume it.


## Evidence bars (v0.4)

Calibrated against 134 hours of professional play, where the naive rules were
wrong loudly: R1 fired 85 times in one episode with zero valid findings, and
R7 flagged 115 dead-air gaps of which ~5 were real.

- **R1** now requires a GM-directed question (GM named, rules lexicon, or
  adjacency to a GM beat) **and** a waiting table — if other players carry on,
  nobody was blocked. Knob: `question_requires_gm_address`.
- **R7** exempts the yielded floor: a GM holding back while players talk is
  craft, not absence. Knobs: `dead_air_requires_quiet_table`,
  `thresholds.quiet_table_max_messages` (default 3).
- **R4** seats gain `aliases` — professional cues are in-fiction by character
  name ~10:1, so the referee must recognise the character's name as a cue.

One narrowing, stated plainly: R1 no longer fires when another player answers
in the GM's place — at a busy table that is textually indistinguishable from
the banter that produced the false-accusation storm, and D1 chooses silence.
