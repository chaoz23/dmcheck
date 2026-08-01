"""DMC-001: every public transport shares one fail-closed input contract."""

import contextlib
import copy
import io
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from unittest import mock

from dmcheck import (apply_charter_overrides, evaluate, evaluate_paths,
                     load_charter, public_charter_digest)
from dmcheck.cli import main as cli_main
from dmcheck.craft import evaluate as evaluate_craft
from dmcheck.mcp import _call as mcp_call
from dmcheck.validation import canonical_charter_digest
from dmcheck.watch import Watcher, watch_main


ROOT = pathlib.Path(__file__).resolve().parent.parent


def charter(**updates):
    value = load_charter()
    value.pop("charter_digest", None)
    value["gm"] = ["GM"]
    value["rules_enabled"] = ["R1"]
    for key, update in updates.items():
        value[key] = update
    return value


def rows(*values):
    return list(values)


def captured_cli(arguments):
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = cli_main(arguments)
    return code, stdout.getvalue(), stderr.getvalue()


class TestTypedEvaluationResult(unittest.TestCase):
    def test_clean_findings_invalid_and_incomplete_are_distinct(self):
        clean = evaluate(rows(
            {"ts": 1, "author": "GM", "content": "The door opens."},
            {"ts": 2, "author": "A", "content": "I enter."}), charter())
        self.assertEqual((clean.status, clean.exit_code), ("clean", 0))

        finding_charter = charter(rules_enabled=["R6"], hidden_terms=["Vecna"])
        finding = evaluate(rows(
            {"ts": 1, "author": "GM", "content": "Vecna waits."}),
            finding_charter)
        self.assertEqual((finding.status, finding.exit_code), ("findings", 1))
        self.assertEqual(finding.findings[0]["rule"], "R6")

        invalid = evaluate(rows(
            {"ts": 1, "author": "GM", "content": 42}), charter())
        self.assertEqual((invalid.status, invalid.exit_code), ("invalid", 2))
        self.assertEqual(invalid.findings, [])

        incomplete = evaluate([], charter())
        self.assertEqual((incomplete.status, incomplete.exit_code),
                         ("incomplete", 2))
        self.assertEqual(incomplete.findings, [])

    def test_legacy_unpacking_cannot_put_errors_in_findings(self):
        from dmcheck import check
        result = check([{"author": "GM", "content": 42}], charter())
        findings, code = result
        self.assertEqual(findings, [])
        self.assertEqual(code, 2)
        self.assertTrue(result.errors)
        self.assertEqual(result[0], [])
        self.assertEqual(result[1], 2)

    def test_mode_is_typed_and_disclosed(self):
        bad = evaluate([], charter(), mode="maybe")
        self.assertEqual(bad.status, "invalid")
        self.assertIsNone(bad.mode)
        self.assertEqual(bad.errors[0].code, "evaluation.mode")
        live = evaluate(rows(
            {"ts": 1, "author": "GM", "content": "Ready."}),
            charter(), mode="live", now=2)
        self.assertEqual(live.mode, "live")


class TestTranscriptValidationMatrix(unittest.TestCase):
    def test_structural_and_content_types_fail_closed(self):
        cases = [
            (None, "transcript.type"),
            ({}, "transcript.type"),
            ("messages", "transcript.type"),
            ([None], "transcript.message_type"),
            ([["GM", "hello"]], "transcript.message_type"),
            ([{"author": "GM"}], "transcript.content_missing"),
            ([{"author": "GM", "content": 42}], "transcript.content_type"),
            ([{"author": None, "content": "x"}], "transcript.author_type"),
            ([{"author": "", "content": "x"}], "transcript.author_empty"),
            ([{"author": "GM", "content": "\ud800"}],
             "transcript.content_unicode"),
            ([{"author": {"name": "GM"}, "content": "x"}],
             "transcript.author_type"),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                result = evaluate(raw, charter())
                self.assertEqual(result.status, "invalid")
                self.assertEqual(result.exit_code, 2)
                self.assertIn(expected, {problem.code for problem in result.errors})
                self.assertEqual(result.findings, [])

    def test_timestamp_boundaries_are_strict(self):
        invalid_values = [True, -1, float("inf"), float("nan"), 10 ** 400,
                          "not-a-date", "2026-08-01T12:00:00",
                          "1969-12-31T23:59:59Z"]
        for value in invalid_values:
            with self.subTest(timestamp=value):
                result = evaluate([
                    {"ts": value, "author": "GM", "content": "x"}
                ], charter())
                self.assertEqual(result.status, "invalid")
                self.assertTrue(any(problem.code.startswith("timestamp.")
                                    for problem in result.errors))

        for value in [0, 1.5, "2026-08-01T12:00:00Z",
                      "2026-08-01T05:00:00-07:00"]:
            with self.subTest(timestamp=value):
                result = evaluate([
                    {"ts": value, "author": "GM", "content": "x"}
                ], charter())
                self.assertNotEqual(result.status, "invalid")

    def test_conflicting_and_mixed_order_timestamps_are_rejected(self):
        conflict = evaluate([
            {"ts": 1, "timestamp": 2, "author": "GM", "content": "x"}
        ], charter())
        self.assertIn("timestamp.conflict",
                      {problem.code for problem in conflict.errors})
        mixed = evaluate([
            {"ts": 1, "author": "GM", "content": "x"},
            {"ts": 3, "author": "A", "content": "x"},
            {"ts": 2, "author": "A", "content": "x"},
        ], charter())
        self.assertIn("transcript.timestamp_order",
                      {problem.code for problem in mixed.errors})
        partial_reverse = evaluate([
            {"ts": 3, "author": "GM", "content": "x"},
            {"author": "A", "content": "x"},
            {"ts": 2, "author": "A", "content": "x"},
        ], charter())
        self.assertIn("transcript.timestamp_order",
                      {problem.code for problem in partial_reverse.errors})
        null_conflict = evaluate([
            {"ts": None, "timestamp": 2, "author": "GM", "content": "x"}
        ], charter())
        self.assertIn("timestamp.conflict",
                      {problem.code for problem in null_conflict.errors})

    def test_reverse_discord_export_normalizes(self):
        result = evaluate([
            {"timestamp": "2026-08-01T12:02:00Z",
             "author": {"username": "A"}, "content": "I enter."},
            {"timestamp": "2026-08-01T12:01:00Z",
             "author": {"username": "GM"}, "content": "The door opens."},
        ], charter())
        self.assertEqual(result.status, "clean")
        self.assertEqual(result.messages, 2)


class TestCharterValidationMatrix(unittest.TestCase):
    def _result(self, raw):
        return evaluate([
            {"ts": 1, "author": "GM", "content": "x"}
        ], raw)

    def test_root_and_collection_types(self):
        cases = [
            (None, "charter.type"),
            ([], "charter.type"),
            ("charter", "charter.type"),
            ({**charter(), "gm": "GM"}, "charter.type"),
            ({**charter(), "hidden_terms": "Vecna"}, "charter.type"),
            ({**charter(), "rules_enabled": "R1"}, "charter.type"),
            ({**charter(), "seats": []}, "charter.type"),
            ({**charter(), "effective_date": None}, "charter.type"),
            ({**charter(), "charter_digest": None}, "charter.digest"),
            ({**charter(), 7: "not-json-compatible"}, "charter.key_type"),
        ]
        for raw, expected in cases:
            with self.subTest(expected=expected):
                result = self._result(raw)
                self.assertEqual(result.status, "invalid")
                self.assertIn(expected, {problem.code for problem in result.errors})

    def test_numeric_boolean_and_rule_boundaries(self):
        cases = []
        for value in [True, 1.5, 0, -1]:
            value_charter = charter()
            value_charter["thresholds"] = {
                **value_charter["thresholds"],
                "answer_within_messages": value,
            }
            cases.append((value_charter, "charter.integer_threshold"))
        for value in [True, -1, float("inf"), float("nan"), 10 ** 400]:
            value_charter = charter()
            value_charter["thresholds"] = {
                **value_charter["thresholds"], "dead_air_seconds": value,
            }
            cases.append((value_charter, "charter.duration_threshold"))
        cases.append(({**charter(), "rules_enabled": ["R99"]},
                      "charter.unknown_rule"))
        cases.append(({**charter(), "rules_enabled": ["R1", "R1"]},
                      "charter.duplicate"))
        for raw, expected in cases:
            with self.subTest(expected=expected):
                result = self._result(raw)
                self.assertEqual(result.status, "invalid")
                self.assertIn(expected, {problem.code for problem in result.errors})

    def test_aliases_are_arrays_and_identities_cannot_collide(self):
        aliases_string = charter(seats={"Ash": {"aliases": "Shalia"}})
        result = self._result(aliases_string)
        self.assertEqual(result.status, "invalid")
        self.assertIn("charter.type", {problem.code for problem in result.errors})

        collision = charter(seats={
            "Ash": {"aliases": ["Shalia"]},
            "Shalia": {"aliases": []},
        })
        result = self._result(collision)
        self.assertIn("charter.alias_collision",
                      {problem.code for problem in result.errors})

        null_mention = charter(seats={"Ash": {"mention": None}})
        result = self._result(null_mention)
        self.assertIn("charter.empty_string",
                      {problem.code for problem in result.errors})

    def test_digest_mismatch_is_invalid(self):
        value = load_charter()
        value["gm"] = ["GM"]
        result = self._result(value)
        self.assertEqual(result.status, "invalid")
        self.assertIn("charter.digest_mismatch",
                      {problem.code for problem in result.errors})

    def test_public_override_helper_recomputes_effective_digest(self):
        value = apply_charter_overrides(load_charter(), gm=["GM"])
        result = self._result(value)
        self.assertNotEqual(result.status, "invalid")
        self.assertNotEqual(result.charter["charter_digest"],
                            load_charter()["charter_digest"])


class TestEvidenceCompleteness(unittest.TestCase):
    def test_empty_all_blank_zero_gm_and_zero_eligible_never_clean(self):
        cases = [
            ([], charter(), None, "transcript.empty"),
            ([{"author": "GM", "content": ""},
              {"author": "A", "content": "  "}], charter(), None,
             "transcript.no_effective_content"),
            ([{"author": "Player", "content": "hello"}], charter(), None,
             "evidence.gm_unobserved"),
            ([{"author": "GM", "content": "hello"}],
             charter(rules_enabled=[]), None, "evidence.no_eligible_rules"),
            ([{"author": "GM", "content": "hello"}],
             charter(rules_enabled=["R1"]), None,
             "evidence.no_eligible_rules"),
            ([{"author": "GM", "content": "hello"}],
             charter(rules_enabled=["R2"]), None,
             "evidence.no_eligible_rules"),
            ([{"author": "GM", "content": "hello"}],
             charter(rules_enabled=["R3"]), None,
             "evidence.no_eligible_rules"),
            ([{"author": "GM", "content": "hello"},
              {"author": "A", "content": "wait"}],
             charter(rules_enabled=["R7"]), None,
             "evidence.no_eligible_rules"),
        ]
        for transcript, table_charter, ledger, expected in cases:
            with self.subTest(expected=expected):
                result = evaluate(transcript, table_charter, ledger)
                self.assertEqual(result.status, "incomplete")
                self.assertEqual(result.exit_code, 2)
                self.assertIn(expected, {problem.code for problem in result.errors})
                self.assertEqual(result.findings, [])

    def test_missing_timestamps_disclose_affected_rule(self):
        result = evaluate([
            {"author": "GM", "content": "hello"},
            {"author": "A", "content": "wait"},
        ], charter(rules_enabled=["R7"]))
        self.assertEqual(result.status, "incomplete")
        self.assertEqual(result.skipped_rules[0]["rule"], "R7")
        self.assertEqual(result.skipped_rules[0]["code"],
                         "timestamped_messages.unavailable")

    def test_rule_eligibility_does_not_depend_on_rule_order(self):
        result = evaluate([
            {"ts": 1, "author": "GM", "content": "hello"},
            {"ts": 2, "author": "A", "content": "I wait"},
        ], charter(rules_enabled=["R8", "R1"]))
        self.assertEqual(result.status, "clean")
        self.assertEqual(set(result.eligible_rules), {"R1", "R8"})

    def test_partially_compatible_time_and_ledger_rules_are_skipped(self):
        cases = [
            (
                [{"ts": 1, "author": "GM", "content": "First."},
                 {"author": "GM", "content": "Narrated, time unknown."}],
                charter(rules_enabled=["R3"]),
                [{"ts": 2, "type": "event", "text": "door opens"}],
                "R3",
            ),
            (
                [{"author": "GM", "content": "Shalia, go."},
                 {"ts": 3, "author": "GM", "content": "What happens?"}],
                charter(rules_enabled=["R4"], seats={"Shalia": {}}),
                [{"ts": 2, "type": "turn", "actor": "Shalia"}],
                "R4",
            ),
            (
                [{"ts": 1, "author": "GM", "content": "Ready."}],
                charter(rules_enabled=["R5"]),
                [{"type": "act", "actor": "A"},
                 {"type": "turn", "actor": "B"}],
                "R5",
            ),
            (
                [{"ts": 1, "author": "GM", "content": "Ready."},
                 {"ts": 2, "author": "A", "content": "I wait."}],
                charter(rules_enabled=["R7"]),
                None,
                "R7",
            ),
            (
                [{"ts": 1, "author": "A", "content": "I wait."},
                 {"author": "GM", "content": "Immediate reply."},
                 {"ts": 1000, "author": "GM", "content": "Later."}],
                charter(rules_enabled=["R7"]),
                None,
                "R7",
            ),
        ]
        for transcript, table_charter, ledger, skipped_rule in cases:
            with self.subTest(rule=skipped_rule):
                result = evaluate(transcript, table_charter, ledger)
                self.assertEqual(result.status, "incomplete")
                self.assertEqual(result.findings, [])
                self.assertEqual(result.eligible_rules, [])
                self.assertIn(skipped_rule,
                              {item["rule"] for item in result.skipped_rules})

    def test_ledger_types_fail_closed(self):
        invalid_ledgers = [
            {}, [None], [{"type": "spell"}],
            [{"type": "turn", "actor": 42}],
            [{"type": "event", "text": False}],
            [{"type": "event", "ts": "2026-08-01T12:00:00"}],
        ]
        for ledger in invalid_ledgers:
            with self.subTest(ledger=ledger):
                result = evaluate([
                    {"ts": 1, "author": "GM", "content": "x"}
                ], charter(), ledger)
                self.assertEqual(result.status, "invalid")
                self.assertEqual(result.findings, [])


class TestTransportParity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="dmcheck-dmc001-")
        self.root = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, name, value):
        path = self.root / name
        path.write_text(value, encoding="utf-8")
        return path

    def test_file_cli_mcp_and_api_share_invalid_codes(self):
        transcript = self._write(
            "bad.jsonl", json.dumps({"ts": 1, "author": "GM", "content": 42}))
        table_charter = self._write("charter.json", json.dumps(charter()))

        file_result = evaluate_paths(str(transcript), str(table_charter))
        cli_code, stdout, stderr = captured_cli(
            ["run", str(transcript), "--charter", str(table_charter)])
        cli_result = json.loads(stdout)
        mcp_result = mcp_call("run", {
            "transcript_path": str(transcript),
            "charter_path": str(table_charter),
        }, allowed_roots=[str(self.root)])

        expected_codes = {problem.code for problem in file_result.errors}
        self.assertEqual(expected_codes, {"transcript.content_type"})
        self.assertEqual(cli_code, 2)
        self.assertEqual(cli_result["status"], "invalid")
        self.assertEqual({item["code"] for item in cli_result["errors"]},
                         expected_codes)
        self.assertEqual({item["code"] for item in mcp_result["errors"]},
                         expected_codes)
        self.assertNotIn("Traceback", stdout + stderr)

        events = []
        watcher = Watcher(charter(), emit=events.append)
        watched = watcher.feed({"ts": 1, "author": "GM", "content": 42})
        self.assertEqual(watched.status, "invalid")
        self.assertEqual({problem.code for problem in watched.errors}, expected_codes)
        self.assertEqual(watcher.close(), 2)

    def test_watch_semantic_error_pointer_matches_file_index(self):
        raw = [
            {"ts": 1, "author": "GM", "content": "valid"},
            {"ts": 2, "author": "A", "content": 42},
        ]
        transcript = self._write(
            "bad-second.jsonl", "\n".join(json.dumps(item) for item in raw))
        table_charter = self._write("charter.json", json.dumps(charter()))
        file_result = evaluate_paths(str(transcript), str(table_charter))
        watcher = Watcher(charter(), emit=lambda event: None)
        watcher.feed(raw[0], now=1)
        watched = watcher.feed(raw[1], now=2)
        self.assertEqual([problem.pointer for problem in watched.errors],
                         [problem.pointer for problem in file_result.errors])

    def test_watch_drops_open_assertions_when_stream_becomes_invalid(self):
        table_charter = charter(rules_enabled=["R7"])
        table_charter["thresholds"] = {
            **table_charter["thresholds"], "dead_air_seconds": 5,
        }
        events = []
        watcher = Watcher(table_charter, emit=events.append)
        watcher.feed({"ts": 0, "author": "GM", "content": "Ready."}, now=0)
        watcher.feed({"ts": 1, "author": "A", "content": "I wait."}, now=1)
        watcher.tick(now=10)
        self.assertTrue(watcher.open)

        failed = watcher.feed({"ts": 11, "author": "GM", "content": 42},
                              now=11)
        self.assertEqual(failed.status, "invalid")
        self.assertEqual(watcher.open, {})
        self.assertEqual(watcher.close(), 2)
        self.assertEqual(events[-1]["open_count"], 0)
        self.assertEqual(events[-1]["findings"], [])

    def test_json_array_jsonl_mcp_and_api_normalize_identically(self):
        raw = [
            {"timestamp": "2026-08-01T12:01:00Z",
             "author": {"username": "GM"}, "content": "The door opens."},
            {"timestamp": "2026-08-01T12:02:00Z",
             "author": {"username": "A"}, "content": "I enter."},
        ]
        table_charter = self._write("charter.json", json.dumps(charter()))
        array_path = self._write("session.json", json.dumps(raw))
        jsonl_path = self._write("session.jsonl", "\n".join(
            json.dumps(value) for value in raw) + "\n")

        direct = evaluate(raw, charter()).to_dict()
        array = evaluate_paths(str(array_path), str(table_charter)).to_dict()
        jsonl = evaluate_paths(str(jsonl_path), str(table_charter)).to_dict()
        mcp = mcp_call("run", {"transcript_path": str(array_path),
                               "charter_path": str(table_charter)},
                       allowed_roots=[str(self.root)])
        for result in (array, jsonl, mcp):
            self.assertEqual(result["status"], direct["status"])
            self.assertEqual(result["messages"], direct["messages"])
            self.assertEqual(result["findings"], direct["findings"])
            self.assertEqual(result["charter"], direct["charter"])

    def test_transport_overrides_replace_invalid_stored_fields_first(self):
        raw_charter = charter()
        raw_charter["gm"] = "not-an-array"
        raw_charter["dice_authors"] = 42
        table_charter = self._write("override-charter.json",
                                    json.dumps(raw_charter))
        transcript = self._write(
            "override-session.jsonl",
            "\n".join([
                json.dumps({"ts": 1, "author": "GM", "content": "Ready."}),
                json.dumps({"ts": 2, "author": "Player",
                            "content": "I enter."}),
            ]))

        without_overrides = evaluate_paths(str(transcript), str(table_charter))
        self.assertEqual(without_overrides.status, "invalid")
        self.assertEqual(
            {problem.pointer for problem in without_overrides.errors},
            {"/gm", "/dice_authors"})

        expected = evaluate_paths(
            str(transcript), str(table_charter), gm=["GM"],
            dice_authors=["DiceBot"])
        cli_code, stdout, stderr = captured_cli([
            "run", str(transcript), "--charter", str(table_charter),
            "--gm", "GM", "--dice-bot", "DiceBot",
        ])
        mcp_result = mcp_call("run", {
            "transcript_path": str(transcript),
            "charter_path": str(table_charter),
            "gm": ["GM"],
            "dice_authors": ["DiceBot"],
        }, allowed_roots=[str(self.root)])
        self.assertEqual(expected.status, "clean")
        self.assertEqual((cli_code, json.loads(stdout)["status"]),
                         (0, "clean"))
        self.assertEqual(mcp_result["status"], "clean")
        self.assertNotIn("Traceback", stdout + stderr)
        effective = load_charter(str(table_charter), gm=["GM"],
                                 dice_authors=["DiceBot"])
        self.assertEqual(expected.charter["gm"], ["GM"])
        self.assertEqual(expected.charter["dice_authors"], ["DiceBot"])
        for result in (expected.to_dict(), json.loads(stdout), mcp_result):
            self.assertEqual(result["charter"]["digest"],
                             public_charter_digest(effective))
            self.assertEqual(
                result["charter"]["digest_scope"],
                "public-policy; hidden values withheld")

    def test_watch_file_and_stdin_empty_are_incomplete_exit_two(self):
        empty = self._write("empty.jsonl", "\n\n")
        args = Namespace(
            transcript=str(empty), charter=None, ledger=None, gm=["GM"],
            dice_bot=None, notify_cmd=None, craft=False, scene="SOCIAL",
            pc=None, follow=False, interval=1.0)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = watch_main(args)
        final = json.loads(output.getvalue().splitlines()[-1])
        self.assertEqual((code, final["status"]), (2, "incomplete"))
        self.assertEqual(final["result_schema_version"], "1.0")

        args.transcript = "-"
        output = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO("\n")), \
                contextlib.redirect_stdout(output):
            code = watch_main(args)
        final = json.loads(output.getvalue().splitlines()[-1])
        self.assertEqual((code, final["status"]), (2, "incomplete"))

    def test_watch_file_and_stdin_normalize_like_closed_api(self):
        raw = [
            {"timestamp": "2026-08-01T12:01:00Z",
             "author": {"username": "GM"}, "content": "The door opens."},
            {"timestamp": "2026-08-01T12:02:00Z",
             "author": {"username": "A"}, "content": "I enter."},
        ]
        transcript = self._write(
            "watch-parity.jsonl",
            "\n".join(json.dumps(message) for message in raw) + "\n")
        table_charter = self._write("watch-charter.json", json.dumps(charter()))
        expected = evaluate_paths(str(transcript), str(table_charter)).to_dict()
        args = Namespace(
            transcript=str(transcript), charter=str(table_charter), ledger=None,
            gm=None, dice_bot=None, notify_cmd=None, craft=False,
            scene="SOCIAL", pc=None, follow=False, interval=1.0)

        results = []
        for source in (str(transcript), "-"):
            args.transcript = source
            output = io.StringIO()
            stdin = io.StringIO(transcript.read_text(encoding="utf-8"))
            with mock.patch("sys.stdin", stdin), \
                    contextlib.redirect_stdout(output):
                code = watch_main(args)
            final = json.loads(output.getvalue().splitlines()[-1])
            self.assertEqual(code, expected["exit_code"])
            results.append(final)

        for result in results:
            self.assertEqual(result["result_schema_version"], "1.0")
            for key in ("status", "messages", "findings", "errors",
                        "eligible_rules", "skipped_rules", "charter"):
                self.assertEqual(result[key], expected[key])

    def test_malformed_inputs_never_print_tracebacks(self):
        bad = self._write("bad.json", "{")
        commands = [
            [sys.executable, "-m", "dmcheck.cli", "run", str(bad), "--gm", "GM"],
            [sys.executable, "-m", "dmcheck.cli", "lint-charter", str(bad)],
            [sys.executable, "-m", "dmcheck.cli", "craft", str(bad)],
        ]
        for command in commands:
            with self.subTest(command=command[-2:]):
                proc = subprocess.run(command, cwd=ROOT, capture_output=True,
                                      text=True)
                self.assertEqual(proc.returncode, 2)
                self.assertNotIn("Traceback", proc.stdout + proc.stderr)
                payload = json.loads(proc.stdout)
                self.assertEqual(payload["status"], "invalid")
                if command[-2] == "craft":
                    self.assertFalse(payload["authoritative"])

        for name, content in (
                ("deep.json", "[" * 1100 + "]" * 1100),
                ("nonfinite.jsonl",
                 '{"ts":NaN,"author":"GM","content":"x"}')):
            path = self._write(name, content)
            proc = subprocess.run(
                [sys.executable, "-m", "dmcheck.cli", "run", str(path),
                 "--gm", "GM"], cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 2)
            self.assertNotIn("Traceback", proc.stdout + proc.stderr)
            self.assertEqual(json.loads(proc.stdout)["errors"][0]["code"],
                             "input.invalid_json")

    def test_missing_cli_paths_are_typed_invalid_results(self):
        for arguments in (["run"], ["watch"], ["craft"]):
            with self.subTest(arguments=arguments):
                code, stdout, stderr = captured_cli(arguments)
                self.assertEqual(code, 2)
                self.assertEqual(json.loads(stdout)["status"], "invalid")
                self.assertNotIn("Traceback", stdout + stderr)

    def test_mcp_rejects_nonobject_requests_without_tracebacks(self):
        proc = subprocess.run(
            [sys.executable, "-m", "dmcheck.mcp"], cwd=ROOT,
            input="[]\n{\n", capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0)
        self.assertNotIn("Traceback", proc.stdout + proc.stderr)
        responses = [json.loads(line) for line in proc.stdout.splitlines()]
        self.assertEqual([response["error"]["code"] for response in responses],
                         [-32600, -32700])

    def test_file_and_stdin_utf8_failures_are_typed(self):
        bad = self.root / "not-utf8.jsonl"
        bad.write_bytes(b"\xff\n")
        result = evaluate_paths(str(bad), gm=["GM"])
        self.assertEqual(result.status, "invalid")
        self.assertEqual(result.errors[0].code, "input.utf8")

        proc = subprocess.run(
            [sys.executable, "-m", "dmcheck.cli", "watch", "-", "--gm", "GM"],
            cwd=ROOT, input=b"\xff\n", capture_output=True)
        output = proc.stdout.decode("utf-8")
        self.assertEqual(proc.returncode, 2)
        self.assertNotIn("Traceback", output + proc.stderr.decode("utf-8"))
        final = json.loads(output.splitlines()[-1])
        self.assertEqual(final["errors"][0]["code"], "input.utf8")

        valid = self._write(
            "valid.jsonl",
            json.dumps({"ts": 1, "author": "GM", "content": "Ready."}))
        missing_ledger = evaluate_paths(str(valid), ledger_path="", gm=["GM"])
        self.assertEqual(missing_ledger.status, "invalid")
        self.assertEqual(missing_ledger.errors[0].pointer, "/ledger")


class TestCraftFailClosed(unittest.TestCase):
    def test_empty_all_dropped_and_bad_beats_are_not_advisory_success(self):
        for raw, status in [([], "incomplete"), (["", "  "], "incomplete"),
                            ([42], "invalid"), (None, "invalid")]:
            with self.subTest(raw=raw):
                result = evaluate_craft(raw)
                self.assertEqual(result["status"], status)
                self.assertEqual(result["exit_code"], 2)
                self.assertFalse(result["authoritative"])

    def test_transcript_with_zero_effective_gm_is_incomplete(self):
        result = evaluate_craft([
            {"ts": 1, "author": "Player", "content": "I act."}
        ], gm_authors=["GM"])
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["errors"][0]["code"],
                         "craft.no_effective_gm_beats")

    def test_valid_craft_is_explicitly_advisory(self):
        result = evaluate_craft(["A short beat.", "Another beat.", "A third beat."])
        self.assertEqual(result["status"], "advisory")
        self.assertEqual(result["exit_code"], 0)
        self.assertFalse(result["authoritative"])


class TestSchemaAndDefaultParity(unittest.TestCase):
    def test_packaged_default_has_verified_schema_version_and_digest(self):
        path = ROOT / "dmcheck" / "default_charter.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        schema = json.loads((ROOT / "dmcheck" / "charter.schema.json").read_text(
            encoding="utf-8"))
        self.assertEqual(value["$schema"], schema["$id"])
        self.assertEqual(value["schema_version"], "1.0")
        self.assertEqual(value["charter_digest"],
                         canonical_charter_digest(value))
        self.assertFalse((ROOT / "charters" / "default.json").exists())
        from dmcheck.validation import DEFAULT_CHARTER_RELEASES
        self.assertEqual(
            DEFAULT_CHARTER_RELEASES[(value["schema_version"],
                                      value["charter_version"])],
            value["charter_digest"])

    def test_all_published_schemas_are_valid_json_with_stable_ids(self):
        for name in ("charter.schema.json", "transcript.schema.json",
                     "ledger.schema.json", "evaluation-result.schema.json"):
            with self.subTest(name=name):
                value = json.loads((ROOT / "dmcheck" / name).read_text(
                    encoding="utf-8"))
                self.assertEqual(value["$schema"],
                                 "https://json-schema.org/draft/2020-12/schema")
                self.assertIn("dmcheck", value["$id"])

    def test_init_uses_the_canonical_schema_and_lints(self):
        with tempfile.TemporaryDirectory(prefix="dmcheck-init-") as temp:
            path = pathlib.Path(temp) / "charter.json"
            code, stdout, stderr = captured_cli(
                ["init", str(path), "--gm", "GM"])
            self.assertEqual(code, 0, stderr)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema_version"], "1.0")
            self.assertEqual(payload["charter_digest"],
                             canonical_charter_digest(payload))
            lint_code, lint_stdout, _ = captured_cli(
                ["lint-charter", str(path)])
            self.assertEqual(lint_code, 0)
            self.assertTrue(json.loads(lint_stdout)["ok"])


if __name__ == "__main__":
    unittest.main()
