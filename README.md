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
 "result_schema_version": "1.0",
 "status": "findings",
 "exit_code": 1,
 "mode": "closed",
 "messages": 9,
 "findings": [
  {"rule": "R2", "summary": "unconsumed-roll: a dice result got no correlated GM narration within threshold",
   "charter": "roll_ack_within_messages=4; correlation=explicit",
   "detail": "dice result from DiceBot received no correlated GM narration within 4 messages",
   "evidence": {"index": 6, "author": "DiceBot", "obligation_id": "roll-42"},
   "status": "open", "severity": "finding", "provenance": "observed",
   "charter_digest": "sha256:...", "effective_policy": {"roll_ack_within_messages": 4, "...": "..."}},
  ...
 ],
 "counts": {"R1": 1, "R2": 1, "R6": 1, "R7": 1, "R8": 1}
}
```

Transcript formats: UTF-8 JSONL of `{ts, author, content}`, or a JSON array of Discord-API-shaped messages (`{timestamp, author: {username}, content}`) in chronological or reverse-chronological order. Authors and content are strings. A supplied timestamp must be a finite nonnegative epoch number or a timezone-aware ISO-8601 string; malformed timestamps are rejected rather than silently disabling time-based rules. Source adapters should preserve immutable `id`, `audience`, `reply_to`/`correlation_id`, and `roll_id` fields so dmcheck can prove which question, roll, or event a response closes; ordinary later GM chatter is not treated as an answer.

## The rule set (seven active rules; one retired compatibility id)

| Rule | Fires when | Origin story |
|---|---|---|
| R1 | an explicitly GM-directed question got no correlated response within threshold | a player asked the DM a lore question; another *player* ended up answering |
| R2 | a real dice result got no correlated GM narration within threshold | "did I hit?" — a player's successful attack roll sat unacknowledged |
| R3 | an engine event got no correlated GM narration | the state engine resolved a hit the table never heard about |
| R4 | a turn began and the GM never addressed the actor by name | "isn't it her turn?" — asked by a player, which is one player too many |
| R5 | **retired; never fires** | actor != turn owner falsely accused legal Reactions and interrupts |
| R6 | a configured hidden term appeared in a GM message; ordinary output exposes only its opaque ID | a module's secret state names leaked into narration |
| R7 | GM dead air beyond threshold while a player waited | 30 seconds reads as thinking; five minutes reads as absence |
| R8 | the session ended with currently open, source-observed R1–R3 obligations; inferred legacy advisories are not promoted | sessions should end in a defined state — that's what makes the next one possible |

The active rules came from running a hybrid table — human and AI players, an AI GM — on Discord, where these failures actually happened and got codified the same week. They apply equally to human GMs: run dmcheck over your own exported game log and see what your table's transcript says.

R5 remains addressable only so old charters and agent integrations do not
break. It has no evaluator and is not enabled by default: an actor differing
from the turn owner can describe a Reaction, a Ready trigger, an opportunity
attack, a legendary or lair action, a controlled creature, an environmental
actor, or another system's interrupt. The ledger does not establish legality.
Any replacement based on an explicit authoritative decision is deferred to
the shared PORT-002 event contract; dmcheck will evaluate only the resulting
communication or recovery obligation.

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
{"event": "open", "rule": "R2", "detail": "dice result from RollBot received no correlated GM narration", ...}
{"event": "resolved", "rule": "R3", ...}
{"event": "session_end", "status": "findings", "exit_code": 1, "open": ["R1"], "open_count": 1, ...}
$ dmcheck explain R2            # the rule, its charter knobs, and the table failure that earned it
$ dmcheck lint-charter my.json  # unknown keys / bad thresholds refuse loudly
```

The point of live: every failure the rules encode was recoverable in the
moment it happened — the unanswered question, the stale roll, the missing cue
all had a seconds-wide window where a nudge saved the beat. Post-hoc tells you
what went wrong last night; watch taps the GM's shoulder before the player
feels it.

## The charter is config, not code

`dmcheck/default_charter.json` is the single authoritative packaged default. It carries `schema_version`, `charter_version`, and a verified SHA-256 `charter_digest`; checkout and wheel execution load that same resource. Its digest is release-locked to the schema/charter version pair, so a changed packaged default refuses to load until its version migration is declared. Override any of it — cue conventions, dead-air tolerance, dice-bot names, hidden-term lists — and version it. Hidden terms may be strings for compatibility or `{id, value}` objects; opaque host-issued IDs are preferred. Finding, watch-hook, CLI, and MCP output withholds both the configured value and raw matching excerpt. A league or organized-play program could publish a charter the way they publish a player's guide; dmcheck then referees any table against it.

```console
$ dmcheck run session.jsonl --charter our-table.json --ledger events.jsonl
$ dmcheck rules            # the rule set with definitions
$ dmcheck charter          # inspect effective public policy; hidden values are redacted
$ dmcheck run session.jsonl --gm "Rob" --dice-bot "RollBot"   # quick overrides, no file needed
$ dmcheck --schema         # machine-readable I/O contract
```

Direct API callers can use `apply_charter_overrides(load_charter(), gm=["Rob"])`; the helper removes the prior effective digest and the evaluator computes the digest for the overridden charter. Mutating a digested charter without doing so is rejected as a stale configuration rather than silently trusted.

## For agents

- `tool.json` at the repo root; `--schema`; exit codes: `0` clean · `1` findings · `2` charter/input unusable.
- MCP server: `dmcheck-mcp` (stdio) with tools `run` and `rules`.
- Findings are structured JSON with rule id, charter citation, effective machine policy, provenance, human-readable detail, and redacted evidence — built to be consumed by a GM agent that fixes its own procedure between beats.
- Every evaluation returns `status: clean|findings|invalid|incomplete`. Invalid and incomplete outcomes exit 2, carry stable error codes and JSON pointers, and never place errors in `findings`. Empty input, no observed configured GM, or no evidence-eligible enabled rule can never report clean.
- Published package schemas are `charter.schema.json`, `transcript.schema.json`, `ledger.schema.json`, and `evaluation-result.schema.json`. Missing timestamps are disclosed through `skipped_rules`; malformed supplied timestamps are invalid.
- Explicit source IDs are authoritative for correlation. Text-only question/roll/event detection is inferred/advisory, and ambiguous legacy evidence follows D1 (silence rather than accusation). The evaluation-envelope work tracked separately must expose that coverage gap; silence is not proof of complete observation.

## What it does NOT do (on purpose)

- **No rules adjudication** — [srdcheck](https://github.com/chaoz23/srdcheck) may provide cited, advisory rules analysis; the authorized upstream engine plus the DM/table's policy and ruling decide whether an action is legal. dmcheck evaluates only table conduct and communication.
- **No character math** — that's [charactercheck](https://github.com/chaoz23/charactercheck). (charactercheck derives the actor; dmcheck referees the table.)
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
`{ts, type: turn|act|event, id, actor, text}` — one line per engine event. GM
narration carries the matching `correlation_id`. One line is written per
engine event. No
lightweight OSS session-ledger existed when we surveyed (2026-07-26), so
this format is the interchange standard our stack shares: engines tap their
logs into it; `run`, `watch`, and settlement all consume it. `actor` and
`turn` are coordination context only; their mismatch is never proof of an
illegal action or a conduct violation.


## Evidence bars (v0.4)

Full calibration story — including what the naive rules got wrong, the
held-out validation, and the negative results — in
[docs/CALIBRATION.md](docs/CALIBRATION.md).

Calibrated against 134 hours of professional play, where the naive rules were
wrong loudly: R1 fired 85 times in one episode with zero valid findings, and
R7 flagged 115 dead-air gaps of which ~5 were real.

- **R1** prefers explicit source audience and correlation evidence. Explicit
  public/player audience is never promoted to a GM obligation, and unrelated
  later GM text does not close a typed question. Legacy text heuristics (GM
  named, rules lexicon, or adjacency) are inferred/advisory and still require
  a waiting table. Knob: `question_requires_gm_address`.
- **R7** exempts the yielded floor: a GM holding back while players talk is
  craft, not absence. Knobs: `dead_air_requires_quiet_table`,
  `thresholds.quiet_table_max_messages` (default 3).
- **R4** seats gain `aliases` — professional cues are in-fiction by character
  name ~10:1, so the referee must recognise the character's name as a cue.

One narrowing, stated plainly: R1 no longer fires when another player answers
in the GM's place — at a busy table that is textually indistinguishable from
the banter that produced the false-accusation storm, and D1 chooses silence.

## The attention lane (v0.5)

```
dmcheck craft session-beats.json --scene SOCIAL --pc Teodor --pc Shalia
```

Statistics, one attention signal, and categorical defects — **never a score**.
Rates run against the professional envelope (134h, two DMs); `attention` is
ONE scene-weighted signal with resolve-and-move-on, because a five-dial
dashboard cost its author the metric he wasn't watching. Categorical
detectors catch what rates structurally cannot: voicing a player's character,
exposing the inference tree, deferring an adjudication, rolling for the
player — all born from a live second-agent test. Advisory only: it reports,
the DM decides, and overrides are expected exactly when the scene demands it.

### seat_quiet (v0.5.2)

`watch --craft --pc <name>` also emits a `seat_quiet` advisory when a player
seat goes silent across N GM beats (knob: `seat_quiet_gm_beats`, default 3)
while the scene advances. Origin: a human player stepped away and the agent
seats carried the scene to its climax without him. The advisory suggests
checking in and holding irreversible advancement — it never blocks, because a
virtual table's virtue is that it does not stall when someone disappears.

### rule checks (v0.5.4)

`craft` now runs the protocol's machine-checkable rules per session
(testability charter: a rule must state its falsification):
**1a** initiative order posted within 5 beats of onset · **1b** ≥95% of
turn-advance beats name a PC · **11a** combat damage-beat median under 20
words (numbers are the register at pace) · **11b** kills get ceremony ≥3× the
combat median (lexical kill detection — findings are review items, and say so).
Advisory throughout; no score exists.
