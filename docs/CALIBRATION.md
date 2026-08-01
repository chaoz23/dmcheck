# How dmcheck was calibrated: 134 hours of professional play

dmcheck judges table *conduct* — unanswered questions, unconsumed rolls,
missing cues, dead air. Its design contract (D1) says a false accusation is
the unforgivable bug. So before trusting it, we pointed it at the best tables
we could find: **35 episodes of Critical Role across two GMs (Matt Mercer,
Aabria Iyengar), 2015–2021 — 134 hours, 27,117 GM beats.**

## It was wrong, loudly

The naive rules produced **85 findings in a single episode — zero valid.**
- R1 (unanswered question) fired on any player line containing "?". Ground
  truth from labelled data: only ~43% of question-marked player lines are
  aimed at the GM at all, and even genuinely GM-directed questions go
  unanswered ~10% of the time at professional tables while play flows on.
- R7 (dead air) flagged 115 silences over 300s. **~110 of them were the GM
  deliberately yielding the floor while players talked** — craft, not
  absence. About 5 were real.

## What changed (v0.4 "evidence bars")

- R1 fires only for a **GM-directed** question (GM named, rules lexicon, or
  adjacency to a GM beat) **while the table is actually waiting** — if other
  players carry on, nobody was blocked.
- R7 exempts the **yielded floor**: dead air requires a quiet table.
- R4 recognizes **in-fiction cues**: professionals cue players by *character*
  name (clear majority to dominant of all naming), so seats carry aliases.
- One narrowing, stated plainly: an inferred/text-only R1 no longer fires when
  another player answers in the GM's place — at a busy table that is textually
  indistinguishable from banter, and D1 chooses silence over an unprovable
  accusation. Explicit audience plus an immutable obligation ID remains open
  through unrelated concurrent player activity.

## Correlation and privacy containment

Text proximity cannot prove that one message answers another. When an adapter
preserves immutable source IDs, audience, and reply/correlation references,
R1–R3 use those facts: unrelated GM chatter cannot close a question, roll, or
engine event. Explicit public/player audience is not a GM obligation. Legacy
text-only question and roll recognition remains inferred/advisory; when a
later GM message makes the outcome ambiguous, D1 chooses silence. The typed
evaluation envelope must separately report that observation gap, so silence is
not promoted to proof of complete coverage.

R6 output never includes a configured hidden value or its matching excerpt.
Charters may assign opaque IDs to protected terms; every ordinary CLI, MCP,
watch, hook, and finding path exposes only those IDs. The emitted charter
digest intentionally covers public policy with hidden values withheld. It is
not a host attestation or a commitment to the secret values.

## Held-out validation

Numbers derived from a corpus can't be confirmed on the same corpus. On **7
episodes the calibration never saw**: 8 of 11 envelope bands passed on the
holdout median; 3 failed **in the same direction — bands too narrow from
small per-GM samples, never a directional reversal** — and were widened and
annotated (GM median words 10–12 → 8–14; character-name cue ratio "~10:1" →
"clear majority to dominant"; affirmation-rate band widened while the
load-bearing affirm:negate ratio of 3.5–7× held at median 4.7).

CI now enforces the result: a generated professional-shaped table (no
copyrighted text) on which the conduct rules must stay under **one finding
per synthetic hour**. The 85-per-episode failure is permanently
unreproducible without CI failing.

## Negative results we kept

- 3 of 8 craft-labelling experiments failed outright and are recorded.
- A synthetic-player feedback lane rated our DM *above* the professional —
  it measured legibility, not craft — and was cut.
- 5 pieces of classic DM folklore were tested against the corpus: one
  confirmed, one likely medium-specific, three unsupported or confounded by
  first proxies ("describe results, not arithmetic" turned out to be
  aspiration — professionals run combat on bare numbers at pace and bank the
  ceremony for kills).

Only aggregate metrics appear here and in the codebase; no transcript text
is redistributed. Transcripts were used under fair-use analysis for
research; the shipped fixtures are synthetic.
