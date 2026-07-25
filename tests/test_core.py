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
        self.assertEqual(self.rules, ["R1", "R2", "R3", "R4", "R5", "R6", "R7", "R8"])

    def test_r1_names_the_ignored_player(self):
        r1 = [f for f in self.findings if f["rule"] == "R1"]
        self.assertTrue(any("Bram" in f["detail"] for f in r1))

    def test_r5_names_both_parties(self):
        r5 = [f for f in self.findings if f["rule"] == "R5"][0]
        self.assertEqual(r5["evidence"]["actor"], "Bram")
        self.assertEqual(r5["evidence"]["turn_of"], "Mira")

    def test_every_finding_cites_charter(self):
        for f in self.findings:
            self.assertTrue(f["charter"])

    def test_exit_code(self):
        self.assertEqual(self.code, 1)


class TestGuards(unittest.TestCase):
    def test_no_gm_declared_is_unusable(self):
        ch = load_charter(CH)
        ch["gm"] = []
        findings, code = check(load_transcript(os.path.join(FIX, "clean-session.jsonl")), ch)
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
