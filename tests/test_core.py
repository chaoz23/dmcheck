import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dmcheck import check, load_charter, load_ledger, load_transcript  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
CH = os.path.join(FIX, "charter.json")


def run(transcript, ledger=None):
    return check(load_transcript(os.path.join(FIX, transcript)),
                 load_charter(CH),
                 load_ledger(os.path.join(FIX, ledger)) if ledger else None)


class TestCleanSession(unittest.TestCase):
    def test_zero_findings_on_clean_table(self):
        """D1: a clean session must produce SILENCE — no findings at all."""
        findings, code = run("clean-session.jsonl")
        self.assertEqual(findings, [])
        self.assertEqual(code, 0)


class TestMessySession(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.findings, cls.code = run("messy-session.jsonl", "messy-ledger.jsonl")
        cls.rules = sorted({f["rule"] for f in cls.findings})

    def test_all_planted_violations_found(self):
        # v0.4 narrowed R1: Bram asks, then ATTACKS two messages later while
        # Mira chats on - nobody was blocked, so the old R1 plant is now
        # correctly a non-violation (naive "?" fired 85x/episode on
        # professional play with 0 valid; D1 chooses silence).
        # Legacy text/time-only R2/R3 evidence remains advisory, so R8 does
        # not promote those untyped inferences into authoritative open state.
        self.assertEqual(self.rules, ["R2", "R3", "R4", "R6", "R7"])

    def test_r1_suppressed_when_asker_moves_on(self):
        # v0.4 evidence bars: Bram's question is GM-directed, but he attacks
        # two beats later and Mira keeps playing - the table never waited.
        # True R1 fires live in tests/test_evidence_bars.py.
        self.assertEqual([f for f in self.findings if f["rule"] == "R1"], [])

    def test_legacy_r5_charter_does_not_infer_action_legality(self):
        # The fixture charter still names R5 on purpose: old charters must not
        # reactivate the unsafe actor != turn-owner predicate.
        self.assertEqual([f for f in self.findings if f["rule"] == "R5"], [])

    def test_every_finding_cites_charter(self):
        for f in self.findings:
            self.assertTrue(f["charter"])

    def test_exit_code(self):
        self.assertEqual(self.code, 1)


class TestGuards(unittest.TestCase):
    def test_no_gm_declared_is_unusable(self):
        ch = load_charter(CH)
        ch["gm"] = []
        result = check(load_transcript(os.path.join(FIX, "clean-session.jsonl")), ch)
        self.assertEqual(result.status, "invalid")
        self.assertEqual(result.exit_code, 2)
        self.assertEqual(result.findings, [])
        self.assertTrue(any(error.code == "charter.gm.empty"
                            for error in result.errors))


if __name__ == "__main__":
    unittest.main()


class TestLiveMode(unittest.TestCase):
    """v0.2: D1 under liveness — nothing opens until provable; run == watch(closed)."""

    def _msgs(self, path):
        return load_transcript(os.path.join(FIX, path))

    def test_open_mode_holds_fire_until_window_complete(self):
        """A dice roll as the LAST message must NOT open R2 live (window not
        elapsed) — but MUST fire once the session closes."""
        ch = load_charter(CH)
        msgs = self._msgs("clean-session.jsonl")[:5]   # ends on the DiceBot roll
        open_f, _ = check(msgs, ch, closed=False)
        self.assertEqual([f for f in open_f if f["rule"] == "R2"], [])
        closed_f, _ = check(msgs, ch, closed=True)
        self.assertTrue(any(f["rule"] == "R2" for f in closed_f))

    def test_r7_fires_from_wall_clock_silence(self):
        """Dead air is provable by TIME passing with no message at all."""
        ch = load_charter(CH)
        msgs = self._msgs("clean-session.jsonl")[:2]   # ends on Bram's question
        t_last = msgs[-1]["ts"]
        quiet, _ = check(msgs, ch, closed=False, now=t_last + 100)
        self.assertFalse(any(f["rule"] == "R7" for f in quiet))
        loud, _ = check(msgs, ch, closed=False, now=t_last + 400)
        self.assertTrue(any("ongoing" in f["detail"] for f in loud if f["rule"] == "R7"))

    def test_watcher_final_state_equals_run(self):
        from dmcheck.watch import Watcher
        ch = load_charter(CH)
        events = []
        w = Watcher(ch, load_ledger(os.path.join(FIX, "messy-ledger.jsonl")),
                    emit=events.append)
        for m in self._msgs("messy-session.jsonl"):
            w.feed(m, now=m["ts"])
        code = w.close()
        self.assertEqual(code, 1)
        final_rules = sorted({f["rule"] for f in w.open.values()})
        run_f, _ = check(self._msgs("messy-session.jsonl"), ch,
                         load_ledger(os.path.join(FIX, "messy-ledger.jsonl")),
                         closed=True)
        self.assertEqual(final_rules, sorted({f["rule"] for f in run_f}))
        self.assertTrue(any(e["event"] == "session_end" for e in events))

    def test_r3_heals_when_narrated(self):
        """The living rule: an unnarrated engine event resolves when the GM
        finally tells the table."""
        from dmcheck.watch import Watcher
        ch = load_charter(CH)
        ledger = [{"ts": 1700000200, "type": "event", "id": "evt-guard-drops",
                   "text": "guard drops"}]
        events = []
        w = Watcher(ch, ledger, emit=events.append)
        w.feed({"ts": 1700000100, "author": "Greta", "content": "The fight rages."})
        w.tick(now=1700000300)
        self.assertTrue(any(e["event"] == "open" and e["rule"] == "R3" for e in events))
        w.feed({"ts": 1700000400, "author": "Greta",
                "content": "The guard drops in a heap!",
                "correlation_id": "evt-guard-drops"}, now=1700000401)
        self.assertTrue(any(e["event"] == "resolved" and e["rule"] == "R3" for e in events))
