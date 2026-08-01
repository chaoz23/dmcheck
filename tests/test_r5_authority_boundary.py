"""DMC-003: dmcheck observes conduct; it does not infer action legality.

The current ledger can only say that an actor acted after another actor's
turn began. That shape is shared by many legal D&D actions and by equivalent
interrupts in other systems, so actor != turn owner must always stay silent.
"""
import json
import os
import pathlib
import tempfile
import unittest

from dmcheck import RULES, check, load_charter
from dmcheck.cli import main
from dmcheck.core import DEFAULT_RULES


LEGACY_FULL_CHARTER = {
    "gm": ["GM"],
    "dice_authors": [],
    "ooc_markers": [],
    "hidden_terms": [],
    "thresholds": {},
    # Exercise the full conduct suite plus explicitly enabled legacy R5. The
    # R3 may independently emit a low-confidence correlation advisory.
    "rules_enabled": list(RULES),
    "seats": {},
}


class TestR5AuthorityBoundary(unittest.TestCase):
    def assert_actor_mismatch_is_clean(self, turn_owner, actor, text,
                                       charter=None):
        transcript = [{"ts": 12.0, "author": "GM",
                       "content": f"{turn_owner}, your turn continues as "
                                  "the table resolves the beat.", "i": 0}]
        ledger = [
            {"ts": 10.0, "type": "turn", "actor": turn_owner},
            {"ts": 11.0, "type": "act", "actor": actor, "text": text},
        ]
        findings, code = check(
            transcript, dict(charter or LEGACY_FULL_CHARTER), ledger)
        self.assertFalse(any(finding["rule"] == "R5" for finding in findings))
        self.assertFalse(any(finding.get("severity", "finding") == "finding"
                             for finding in findings))
        self.assertIn(code, (0, 1))

    def test_reaction_shape_is_clean(self):
        self.assert_actor_mismatch_is_clean(
            "Ogre", "Wizard", "Wizard casts Shield as a Reaction")

    def test_ready_trigger_shape_is_clean(self):
        self.assert_actor_mismatch_is_clean(
            "Cultist", "Rogue", "Rogue releases a readied shot on its trigger")

    def test_opportunity_attack_shape_is_clean(self):
        self.assert_actor_mismatch_is_clean(
            "Bandit", "Fighter", "Fighter makes an opportunity attack")

    def test_legendary_action_shape_is_clean(self):
        self.assert_actor_mismatch_is_clean(
            "Paladin", "Ancient Dragon", "Dragon uses a legendary action")

    def test_lair_action_shape_is_clean(self):
        self.assert_actor_mismatch_is_clean(
            "Ranger", "Dragon's Lair", "The lair acts on its initiative count")

    def test_controlled_mount_shape_is_clean(self):
        self.assert_actor_mismatch_is_clean(
            "Cavalier", "Warhorse Mount", "The controlled mount takes its action")

    def test_familiar_shape_is_clean(self):
        self.assert_actor_mismatch_is_clean(
            "Druid", "Owl Familiar", "The controlled familiar takes its action")

    def test_environmental_actor_shape_is_clean(self):
        self.assert_actor_mismatch_is_clean(
            "Cleric", "Collapsing Cavern", "Falling stone changes the scene")

    def test_system_neutral_interrupt_shape_is_clean(self):
        self.assert_actor_mismatch_is_clean(
            "Intruder", "Security System", "A countermeasure interrupts the move")

    def test_actor_difference_alone_is_clean_even_without_a_known_exception(self):
        self.assert_actor_mismatch_is_clean(
            "Current Actor", "Different Actor", "Different Actor does something")

    def test_direct_api_defaults_do_not_enable_r5(self):
        charter = dict(LEGACY_FULL_CHARTER)
        charter.pop("rules_enabled")
        self.assert_actor_mismatch_is_clean(
            "Current Actor", "Different Actor", "Different Actor does something",
            charter=charter)

    def test_defaults_exclude_r5_but_legacy_integrations_can_explain_it(self):
        self.assertIn("R5", RULES)
        self.assertIn("retired", RULES["R5"])
        self.assertNotIn("R5", DEFAULT_RULES)
        self.assertNotIn("R5", load_charter()["rules_enabled"])

    def test_canonical_packaged_default_excludes_r5(self):
        root = pathlib.Path(__file__).resolve().parent.parent
        charter = json.loads(
            (root / "dmcheck" / "default_charter.json").read_text())
        self.assertNotIn("R5", charter["rules_enabled"])
        self.assertFalse((root / "charters" / "default.json").exists())

    def test_init_generated_charter_excludes_r5(self):
        from contextlib import redirect_stdout
        from io import StringIO

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "charter.json")
            with redirect_stdout(StringIO()):
                self.assertEqual(main(["init", path, "--gm", "GM"]), 0)
            with open(path) as f:
                charter = json.load(f)
        self.assertNotIn("R5", charter["rules_enabled"])

    def test_explain_r5_remains_compatible_and_states_retirement(self):
        from contextlib import redirect_stdout
        from io import StringIO

        stdout = StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(main(["explain", "R5"]), 0)
        payload = json.loads(stdout.getvalue())
        self.assertIn("retired", payload["definition"])
        self.assertIn("false", payload["origin"])


if __name__ == "__main__":
    unittest.main()
