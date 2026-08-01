import io
import json
import os
import tempfile
import unicodedata
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from dmcheck.cli import main as cli_main
from dmcheck.core import (check, load_transcript, public_charter,
                          redact_output)
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

    def test_public_audience_wins_over_conflicting_question_type(self):
        transcript = rows(
            {"ts": 1, "id": "q-public", "author": "A", "content": "Ready?",
             "event_type": "question.to_gm", "audience": "table"},
            {"ts": 2, "author": "A", "content": "waiting"},
            {"ts": 3, "author": "A", "content": "waiting"},
        )
        self.assertEqual(check(transcript, charter("R1")), ([], 0))

        transcript[0].pop("audience")
        transcript[0]["event_type"] = "question.public"
        self.assertEqual(check(transcript, charter("R1")), ([], 0))

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

    def test_concurrent_typed_questions_remain_distinct_obligations(self):
        transcript = rows(
            {"ts": 1, "id": "q-a", "author": "A",
             "content": "Rules clarification", "event_type": "question.to_gm"},
            {"ts": 2, "id": "q-b", "author": "B",
             "content": "World clarification", "event_type": "question.to_gm"},
            {"ts": 3, "author": "C", "content": "I hold my action."},
        )
        findings, code = check(transcript, charter("R1"))
        self.assertEqual(code, 1)
        self.assertEqual(
            {f["evidence"]["obligation_id"] for f in findings},
            {"q-a", "q-b"})
        self.assertTrue(all(f["provenance"] == "observed" for f in findings))

    def test_late_correlated_answer_resolves_overdue_question(self):
        transcript = rows(
            {"ts": 1, "id": "q-late", "author": "A", "content": "DC?",
             "audience": "GM"},
            {"ts": 2, "author": "B", "content": "waiting"},
            {"ts": 3, "author": "C", "content": "waiting"},
            {"ts": 4, "author": "GM", "content": "It is 15.",
             "correlation_id": "q-late"},
        )
        self.assertEqual(check(transcript, charter("R1", "R8")), ([], 0))

    def test_correlated_status_is_not_a_question_answer(self):
        transcript = rows(
            {"ts": 1, "id": "q-open", "author": "A", "content": "DC?",
             "audience": "GM"},
            {"ts": 2, "author": "GM", "content": "Bot online",
             "event_type": "bot.status", "correlation_id": "q-open"},
        )
        findings, code = check(transcript, charter("R1"))
        self.assertEqual(code, 1)
        self.assertEqual(findings[0]["evidence"]["obligation_id"], "q-open")

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

    def test_typed_bot_status_and_request_cannot_masquerade_as_results(self):
        cases = (
            ("bot.status", "Last roll: 1d20 = 20"),
            ("bot.error", "A rolls 1d20 = 20"),
            ("roll.request", "A rolls 1d20 = 20"),
        )
        for event_type, content in cases:
            transcript = rows(
                {"ts": 1, "author": "DiceBot", "content": content,
                 "event_type": event_type, "roll_id": "roll-status"},
            )
            self.assertEqual(check(transcript, charter("R2")), ([], 0),
                             event_type)

    def test_typed_result_is_not_rejected_by_unrelated_word_left(self):
        transcript = rows(
            {"ts": 1, "author": "DiceBot",
             "content": "A rolls 1d20 = 19; the target has 2 HP left",
             "event_type": "roll.result", "roll_id": "roll-real"},
        )
        findings, code = check(transcript, charter("R2"))
        self.assertEqual(code, 1)
        self.assertEqual(findings[0]["evidence"]["obligation_id"], "roll-real")

    def test_gm_authored_typed_result_is_already_narrated(self):
        transcript = rows(
            {"ts": 1, "author": "GM", "content": "I roll 1d20 = 19",
             "event_type": "roll.result", "roll_id": "gm-roll"},
        )
        self.assertEqual(check(transcript, charter("R2", "R8")), ([], 0))

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

    def test_late_correlated_narration_resolves_overdue_roll(self):
        transcript = rows(
            {"ts": 1, "author": "DiceBot", "content": "A rolls 1d20 = 19",
             "event_type": "roll.result", "roll_id": "roll-late"},
            {"ts": 2, "author": "A", "content": "waiting"},
            {"ts": 3, "author": "B", "content": "waiting"},
            {"ts": 4, "author": "GM", "content": "That hits.",
             "roll_id": "roll-late"},
        )
        self.assertEqual(check(transcript, charter("R2", "R8")), ([], 0))

    def test_correlated_status_or_request_is_not_roll_narration(self):
        for event_type in ("bot.status", "bot.error", "roll.request"):
            transcript = rows(
                {"ts": 1, "author": "DiceBot", "content": "A rolls 1d20 = 19",
                 "event_type": "roll.result", "roll_id": "roll-open"},
                {"ts": 2, "author": "GM", "content": "Still processing",
                 "event_type": event_type, "roll_id": "roll-open"},
            )
            findings, code = check(transcript, charter("R2"))
            self.assertEqual(code, 1, event_type)
            self.assertEqual(findings[0]["evidence"]["obligation_id"],
                             "roll-open")

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

    def test_correlated_status_is_not_engine_event_narration(self):
        ledger = [{"ts": 2, "type": "event", "id": "evt-1",
                   "text": "door opens"}]
        transcript = rows(
            {"ts": 3, "author": "GM", "content": "Bot online",
             "event_type": "bot.status", "correlation_id": "evt-1"},
        )
        findings, code = check(transcript, charter("R3"), ledger)
        self.assertEqual(code, 1)
        self.assertEqual(findings[0]["evidence"]["event_id"], "evt-1")

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

    def test_ledger_only_and_missing_timestamp_event_is_open_and_promoted(self):
        ledger = [{"type": "event", "id": "evt-no-ts", "text": "door opens"}]
        findings, code = check([], charter("R3", "R8"), ledger)
        self.assertEqual(code, 1)
        self.assertEqual([f["rule"] for f in findings], ["R3", "R8"])
        self.assertEqual(findings[0]["evidence"]["event_id"], "evt-no-ts")
        self.assertEqual(findings[1]["evidence"]["obligation_ids"],
                         ["evt-no-ts"])

    def test_watcher_uses_source_ids_and_normalizes_raw_discord_rows(self):
        ledger = [
            {"ts": 2, "type": "event", "id": "evt-a", "text": "same"},
            {"ts": 2, "type": "event", "id": "evt-b", "text": "same"},
        ]
        events = []
        watcher = Watcher(charter("R3"), ledger, emit=events.append)
        watcher.tick(now=3)
        opened = [e for e in events
                  if e["event"] == "open" and e["rule"] == "R3"]
        self.assertEqual({e["evidence"]["event_id"] for e in opened},
                         {"evt-a", "evt-b"})

        watcher.feed({
            "timestamp": "1970-01-01T00:00:03Z",
            "author": {"id": "gm-1", "username": "GM", "bot": False},
            "content": "The first event resolves.",
            "correlation_id": "evt-a",
        })
        resolved = [e for e in events
                    if e["event"] == "resolved" and e["rule"] == "R3"]
        self.assertEqual([e["evidence"]["event_id"] for e in resolved],
                         ["evt-a"])
        self.assertEqual(
            {f["evidence"]["event_id"] for f in watcher.open.values()
             if f["rule"] == "R3"},
            {"evt-b"})

    def test_inferred_advisory_has_confidence_but_is_not_promoted_or_notified(self):
        events = []
        with patch("dmcheck.watch.subprocess.run") as notify:
            watcher = Watcher(charter("R2", "R8"), emit=events.append,
                              notify_cmd="local-notifier")
            watcher.feed({"ts": 1, "author": "DiceBot",
                          "content": "A rolls 1d20 = 19"}, now=1)
            watcher.feed({"ts": 2, "author": "A", "content": "waiting"}, now=2)
            watcher.feed({"ts": 3, "author": "A", "content": "waiting"}, now=3)
            code = watcher.close()

        advisory = next(e for e in events
                        if e["event"] == "open" and e.get("rule") == "R2")
        self.assertEqual(advisory["severity"], "advisory")
        self.assertEqual(advisory["provenance"], "inferred")
        self.assertEqual(advisory["confidence"], "low")
        self.assertFalse(any(e.get("rule") == "R8" for e in events))
        session_end = next(e for e in events if e["event"] == "session_end")
        self.assertEqual(session_end["open_count"], 0)
        self.assertEqual(code, 0)
        notify.assert_not_called()


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

    def test_full_unicode_casefold_variant_is_detected_and_withheld(self):
        protected = charter(
            "R6",
            hidden_terms=[{"id": "street-1", "value": "Straße"}],
        )
        findings, code = check(rows(
            {"ts": 1, "author": "GM", "content": "The STRASSE is trapped."},
        ), protected)
        self.assertEqual(code, 1)
        self.assertEqual(findings[0]["evidence"]["secret_ids"], ["street-1"])
        self.assertNotIn("strasse", json.dumps(findings).casefold())

    def test_invisible_format_controls_cannot_bypass_redaction(self):
        protected = charter(
            "R6",
            hidden_terms=[{"id": "street-1", "value": "Straße"}],
        )
        findings, code = check(rows(
            {"ts": 1, "author": "GM",
             "content": "The Stra\u200bße is trapped."},
        ), protected)
        self.assertEqual(code, 1)
        rendered = json.dumps(findings, ensure_ascii=False)
        self.assertNotIn("stra\u200bße", rendered.casefold())
        self.assertEqual(findings[0]["evidence"]["content"], "[REDACTED]")

    def test_low_entropy_term_uses_whole_word_boundary(self):
        protected = charter(hidden_terms=[{"id": "one-letter", "value": "x"}])
        safe = redact_output({"standalone": "Mark X here", "embedded": "exit"},
                             protected)
        self.assertNotIn(" X ", safe["standalone"])
        self.assertEqual(safe["embedded"], "exit")

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

    def test_cli_lint_redacts_secret_from_validation_diagnostics(self):
        protected = charter(
            "R6",
            hidden_terms=[{"id": "street-1", "value": "Straße"}],
            seats={"STRASSE": "invalid"},
        )
        with tempfile.TemporaryDirectory() as td:
            charter_path = os.path.join(td, "charter.json")
            with open(charter_path, "w") as handle:
                json.dump(protected, handle)
            stream = io.StringIO()
            with redirect_stdout(stream):
                code = cli_main(["lint-charter", charter_path])
        self.assertEqual(code, 1)
        self.assertNotIn("strasse", stream.getvalue().casefold())

    def test_cli_run_redacts_top_level_charter_version(self):
        protected = self._charter("R6")
        protected["charter_version"] = self.SECRET_VARIANT
        with tempfile.TemporaryDirectory() as td:
            charter_path = os.path.join(td, "charter.json")
            transcript_path = os.path.join(td, "session.jsonl")
            with open(charter_path, "w") as handle:
                json.dump(protected, handle)
            with open(transcript_path, "w") as handle:
                handle.write(json.dumps({
                    "ts": 1, "author": "GM",
                    "content": f"The {self.SECRET} is below.",
                }) + "\n")
            stream = io.StringIO()
            with redirect_stdout(stream):
                code = cli_main(["run", transcript_path,
                                 "--charter", charter_path])
        self.assertEqual(code, 1)
        self.assert_secret_absent(stream.getvalue())

    def test_failure_outputs_are_redacted_after_charter_load(self):
        with tempfile.TemporaryDirectory() as td:
            charter_path = os.path.join(td, "charter.json")
            missing = os.path.join(td, self.SECRET + ".jsonl")
            with open(charter_path, "w") as handle:
                json.dump(self._charter("R6"), handle)

            for argv, target in (
                    (["run", missing, "--charter", charter_path], "stderr"),
                    (["craft", missing, "--charter", charter_path], "stderr"),
                    (["watch", missing, "--charter", charter_path], "stdout")):
                stream = io.StringIO()
                context = (redirect_stderr(stream) if target == "stderr"
                           else redirect_stdout(stream))
                with context:
                    code = cli_main(argv)
                self.assertEqual(code, 2, argv[0])
                self.assert_secret_absent(stream.getvalue())

            self.assert_secret_absent(_call("run", {
                "charter_path": charter_path,
                "transcript_path": missing,
            }))

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
        self.assertEqual(finding["confidence"], "high")
        self.assertTrue(finding["charter_digest"].startswith("sha256:"))
        self.assertEqual(finding["effective_policy"], {
            "dead_air_seconds": 300,
            "dead_air_requires_quiet_table": True,
            "quiet_table_max_messages": 3,
            "dice_authors": ["DiceBot"],
            "ooc_markers": ["[OOC]"],
        })

    def test_public_digest_tracks_public_policy_not_hidden_values(self):
        base = charter(
            "R7", hidden_terms=[{"id": "room-7", "value": "Dragon"}])
        changed_secret = charter(
            "R7", hidden_terms=[{"id": "room-7", "value": "Owlbear"}])
        changed_policy = charter(
            "R7", hidden_terms=[{"id": "room-7", "value": "Dragon"}])
        changed_policy["thresholds"]["dead_air_seconds"] = 301
        transcript = rows(
            {"ts": 1, "author": "A", "content": "I wait."},
            {"ts": 401, "author": "GM", "content": "Back."},
        )
        first = check(transcript, base)[0][0]["charter_digest"]
        second = check(transcript, changed_secret)[0][0]["charter_digest"]
        third = check(transcript, changed_policy)[0][0]["charter_digest"]
        self.assertEqual(first, second)
        self.assertNotEqual(first, third)


if __name__ == "__main__":
    unittest.main()
