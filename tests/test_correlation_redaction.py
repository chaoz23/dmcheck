import io
import json
import os
import tempfile
import unicodedata
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from dmcheck.cli import main as cli_main
from dmcheck.core import check, load_transcript, public_charter
from dmcheck.mcp import _call
from dmcheck.watch import Watcher


def charter(*rules, **overrides):
    out = {
        "charter_version": "test-1",
        "gm": ["GM"],
        "dice_authors": ["DiceBot"],
        "ooc_markers": ["[OOC]"],
        "hidden_terms": [],
        "thresholds": {
            "answer_within_messages": 2,
            "roll_ack_within_messages": 2,
            "dead_air_seconds": 300,
            "quiet_table_max_messages": 3,
        },
        "rules_enabled": list(rules),
        "seats": {},
        "question_requires_gm_address": True,
        "dead_air_requires_quiet_table": True,
    }
    out.update(overrides)
    return out


def rows(*messages):
    out = [dict(message) for message in messages]
    for index, message in enumerate(out):
        message["i"] = index
    return out


class CorrelationTests(unittest.TestCase):
    def test_loader_preserves_source_identity_and_reply_evidence(self):
        payload = [{
            "id": "m-2",
            "timestamp": "2026-08-01T00:00:00Z",
            "author": {"id": "u-1", "username": "A", "bot": False},
            "content": "answer",
            "referenced_message": {"id": "m-1"},
            "audience": ["GM"],
        }]
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "session.json")
            with open(path, "w") as handle:
                json.dump(payload, handle)
            loaded = load_transcript(path)
        self.assertEqual(loaded[0]["id"], "m-2")
        self.assertEqual(loaded[0]["reply_to"], "m-1")
        self.assertEqual(loaded[0]["author_id"], "u-1")
        self.assertIs(loaded[0]["is_bot"], False)

    def test_public_or_player_question_is_not_a_gm_obligation(self):
        for audience in ("table", "B", ["GM", "table"]):
            transcript = rows(
                {"ts": 1, "id": "q-1", "author": "A", "content": "Ready?",
                 "audience": audience},
                {"ts": 2, "author": "A", "content": "Going once"},
                {"ts": 3, "author": "A", "content": "Going twice"},
            )
            findings, code = check(transcript, charter("R1"))
            self.assertEqual((findings, code), ([], 0), audience)

    def test_unrelated_gm_message_does_not_close_typed_question(self):
        transcript = rows(
            {"ts": 1, "id": "q-1", "author": "A", "content": "What is the DC?",
             "audience": "GM"},
            {"ts": 2, "id": "gm-1", "author": "GM", "content": "The torch gutters."},
            {"ts": 3, "author": "A", "content": "Still waiting."},
        )
        findings, code = check(transcript, charter("R1"))
        self.assertEqual(code, 1)
        r1 = next(f for f in findings if f["rule"] == "R1")
        self.assertEqual(r1["evidence"]["obligation_id"], "q-1")
        self.assertEqual(r1["severity"], "finding")

        transcript[1]["correlation_id"] = "q-1"
        findings, code = check(transcript, charter("R1"))
        self.assertEqual((findings, code), ([], 0))

    def test_text_only_question_preserves_d1_when_response_is_ambiguous(self):
        transcript = rows(
            {"ts": 1, "author": "A", "content": "GM, what is the DC?"},
            {"ts": 2, "author": "GM", "content": "The torch gutters."},
            {"ts": 3, "author": "A", "content": "Still waiting."},
        )
        self.assertEqual(check(transcript, charter("R1")), ([], 0))

    def test_bot_status_damage_and_roll_request_are_not_results(self):
        for content in ("DiceBot online and ready", "Error: permission denied",
                        "14 damage", "Roll 1d20 please"):
            transcript = rows(
                {"ts": 1, "author": "DiceBot", "content": content},
                {"ts": 2, "author": "A", "content": "..."},
                {"ts": 3, "author": "A", "content": "..."},
            )
            self.assertEqual(check(transcript, charter("R2")), ([], 0), content)

    def test_unrelated_gm_message_does_not_close_typed_roll(self):
        transcript = rows(
            {"ts": 1, "author": "DiceBot", "content": "A rolls 1d20+4 = 19",
             "event_type": "roll.result", "roll_id": "roll-7"},
            {"ts": 2, "author": "GM", "content": "The rain gets harder."},
        )
        findings, code = check(transcript, charter("R2"))
        self.assertEqual(code, 1)
        self.assertEqual(findings[0]["evidence"]["obligation_id"], "roll-7")

        transcript[1]["roll_id"] = "roll-7"
        self.assertEqual(check(transcript, charter("R2")), ([], 0))

    def test_mislabeled_bot_error_is_not_a_roll_result(self):
        transcript = rows(
            {"ts": 1, "author": "DiceBot", "content": "Error: try again",
             "event_type": "roll.result", "roll_id": "roll-bad"},
        )
        self.assertEqual(check(transcript, charter("R2")), ([], 0))

    def test_engine_event_requires_matching_narration_reference(self):
        ledger = [{"ts": 2, "type": "event", "id": "evt-1", "text": "door opens"}]
        transcript = rows(
            {"ts": 1, "author": "GM", "content": "Before."},
            {"ts": 3, "author": "GM", "content": "Unrelated weather."},
        )
        findings, code = check(transcript, charter("R3"), ledger)
        self.assertEqual(code, 1)
        self.assertEqual(findings[0]["evidence"]["event_id"], "evt-1")

        transcript[1]["correlation_id"] = "evt-1"
        self.assertEqual(check(transcript, charter("R3"), ledger), ([], 0))

    def test_r8_uses_current_open_state_not_transcript_quartile(self):
        transcript = rows(
            {"ts": 1, "id": "q-old", "author": "A", "content": "What is the DC?",
             "audience": "GM"},
            *({"ts": i, "author": "A", "content": "waiting"} for i in range(2, 11)),
        )
        findings, code = check(transcript, charter("R1", "R8"))
        self.assertEqual(code, 1)
        r8 = next(f for f in findings if f["rule"] == "R8")
        self.assertEqual(r8["evidence"]["open"], ["R1"])
        self.assertEqual(r8["evidence"]["obligation_ids"], ["q-old"])


class RedactionTests(unittest.TestCase):
    SECRET = "Caf\u00e9"
    SECRET_VARIANT = "CAFE\u0301"

    def _charter(self, *rules):
        return charter(*rules, hidden_terms=[{"id": "room-7", "value": self.SECRET}])

    def assert_secret_absent(self, value):
        rendered = json.dumps(value, ensure_ascii=False)
        normalized = unicodedata.normalize("NFKC", rendered).casefold()
        self.assertNotIn(unicodedata.normalize("NFKC", self.SECRET).casefold(),
                         normalized)

    def test_r6_withholds_term_and_raw_excerpt_for_unicode_variant(self):
        transcript = rows(
            {"ts": 1, "author": "GM",
             "content": f"The {self.SECRET_VARIANT} is below."},
        )
        findings, code = check(transcript, self._charter("R6"))
        self.assertEqual(code, 1)
        self.assert_secret_absent(findings)
        self.assertEqual(findings[0]["evidence"]["secret_ids"], ["room-7"])
        self.assertEqual(findings[0]["evidence"]["content"], "[REDACTED]")
        self.assertEqual(findings[0]["effective_policy"]["hidden_term_ids"],
                         ["room-7"])

    def test_secret_is_scrubbed_from_other_rule_evidence(self):
        transcript = rows(
            {"ts": 1, "id": "q-1", "author": "A",
             "content": f"GM, is the {self.SECRET} here?", "audience": "GM"},
            {"ts": 2, "author": "A", "content": "waiting"},
            {"ts": 3, "author": "A", "content": "still waiting"},
        )
        findings, code = check(transcript, self._charter("R1"))
        self.assertEqual(code, 1)
        self.assert_secret_absent(findings)

    def test_public_charter_withholds_values_and_uses_opaque_ids(self):
        safe = public_charter(self._charter("R6"))
        self.assert_secret_absent(safe)
        self.assertEqual(safe["hidden_terms"],
                         [{"id": "room-7", "value": "[REDACTED]"}])

    def test_cli_charter_and_run_do_not_emit_secret(self):
        with tempfile.TemporaryDirectory() as td:
            charter_path = os.path.join(td, "charter.json")
            transcript_path = os.path.join(td, "session.jsonl")
            with open(charter_path, "w") as handle:
                json.dump(self._charter("R6"), handle)
            with open(transcript_path, "w") as handle:
                handle.write(json.dumps({"ts": 1, "author": "GM",
                                         "content": f"The {self.SECRET} is below."}) + "\n")

            for argv in (["charter", "--charter", charter_path],
                         ["run", transcript_path, "--charter", charter_path]):
                stream = io.StringIO()
                with redirect_stdout(stream):
                    cli_main(argv)
                self.assert_secret_absent(stream.getvalue())

    def test_mcp_and_watch_hook_receive_only_redacted_findings(self):
        with tempfile.TemporaryDirectory() as td:
            charter_path = os.path.join(td, "charter.json")
            transcript_path = os.path.join(td, "session.jsonl")
            with open(charter_path, "w") as handle:
                json.dump(self._charter("R6"), handle)
            with open(transcript_path, "w") as handle:
                handle.write(json.dumps({"ts": 1, "author": "GM",
                                         "content": f"The {self.SECRET} is below."}) + "\n")
            self.assert_secret_absent(_call("run", {
                "charter_path": charter_path,
                "transcript_path": transcript_path,
            }))

        emitted = []
        with patch("dmcheck.watch.subprocess.run") as notify:
            watcher = Watcher(self._charter("R6"), emit=emitted.append,
                              notify_cmd="local-notifier")
            watcher.feed({"ts": 1, "author": "GM",
                          "content": f"The {self.SECRET} is below."}, now=1)
        self.assert_secret_absent(emitted)
        self.assert_secret_absent(notify.call_args.kwargs["input"])

    def test_every_finding_has_machine_policy_and_public_charter_digest(self):
        transcript = rows(
            {"ts": 1, "author": "A", "content": "I wait."},
            {"ts": 401, "author": "GM", "content": "Back."},
        )
        findings, code = check(transcript, self._charter("R7"))
        self.assertEqual(code, 1)
        finding = findings[0]
        self.assertEqual(finding["charter_version"], "test-1")
        self.assertTrue(finding["charter_digest"].startswith("sha256:"))
        self.assertEqual(finding["effective_policy"], {
            "dead_air_seconds": 300,
            "dead_air_requires_quiet_table": True,
            "quiet_table_max_messages": 3,
            "dice_authors": ["DiceBot"],
            "ooc_markers": ["[OOC]"],
        })


if __name__ == "__main__":
    unittest.main()
