"""E4 calibration gate: conduct rules must stay near-silent on a synthetic
professional-shaped table (7 seats, banter questions, busy deliberation gaps).
No copyrighted text - the fixture is generated. Ceiling pre-registered:
<= 1 finding per synthetic hour. Origin: naive R1 fired 85x/episode on real
pro play; this test keeps that regression impossible."""
import random
import unittest

from dmcheck.core import check

SEATS = ["A", "B", "C", "D", "E", "F", "G"]
BANTER = ["It's a real poster?", "ha, wild", "no way", "I love this",
          "wait really?", "that's so cool", "ok ok", "did you see that?"]


def synth(hours=2, seed=11):
    random.seed(seed)
    rows, ts = [], 0.0
    for _ in range(int(hours * 400)):
        ts += random.uniform(3, 12)
        who = random.choice(SEATS + ["GM"] * 3)
        txt = random.choice(BANTER) if who != "GM" else "the scene continues on"
        rows.append({"ts": ts, "author": who, "content": txt})
        # deliberation stretch: players talk among themselves for minutes
        if random.random() < 0.02:
            for _ in range(random.randint(5, 12)):
                ts += random.uniform(20, 60)
                rows.append({"ts": ts, "author": random.choice(SEATS),
                             "content": random.choice(BANTER)})
    for i, r in enumerate(rows):
        r["i"] = i
    return rows, ts / 3600


class TestCalibrationCeiling(unittest.TestCase):
    def test_findings_per_hour_ceiling(self):
        ch = {"gm": ["GM"], "dice_authors": [], "ooc_markers": [],
              "hidden_terms": [], "thresholds": {}, "seats": {},
              "rules_enabled": ["R1", "R7"]}
        rows, hours = synth()
        findings, _ = check(rows, ch)
        rate = len(findings) / hours
        self.assertLessEqual(
            rate, 1.0,
            f"{rate:.1f} findings/hour on a professional-shaped synthetic "
            f"table - the evidence bars have regressed")


if __name__ == "__main__":
    unittest.main()
