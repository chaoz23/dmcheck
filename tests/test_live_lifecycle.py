"""DMC-002: deterministic live lifecycle, terminal state, and JSONL cursors."""
import contextlib
import io
import json
import os
import random
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, mock

from dmcheck.core import check, load_charter, load_ledger, load_transcript
from dmcheck.watch import JsonlFollower, Watcher, poll_sources, watch_main


FIX = Path(__file__).parent / "fixtures"


def charter(*rules, **thresholds):
    return {
        "gm": ["GM"],
        "dice_authors": ["DiceBot"],
        "ooc_markers": ["[OOC]"],
        "hidden_terms": [],
        "thresholds": dict(thresholds),
        "rules_enabled": list(rules),
        "seats": {},
        "question_requires_gm_address": True,
        "dead_air_requires_quiet_table": True,
    }


def indexed(rows):
    result = []
    for index, row in enumerate(rows):
        item = dict(row)
        item["i"] = index
        result.append(item)
    return result


def jsonl(row):
    return json.dumps(row, separators=(",", ":")) + "\n"


class TestR4Lifecycle(TestCase):
    def test_waits_for_exactly_configured_qualifying_gm_beats(self):
        ch = charter("R4", cue_within_gm_messages=3)
        events = []
        watcher = Watcher(ch, emit=events.append)
        watcher.feed_ledger(
            {"ts": 100, "type": "turn", "actor": "Ash"}, now=100)
        watcher.feed(
            {"ts": 101, "author": "Player", "content": "waiting"}, now=101)
        watcher.feed(
            {"ts": 102, "author": "GM", "content": "The door opens."}, now=102)
        watcher.feed(
            {"ts": 103, "author": "DiceBot", "content": "1d20=12"}, now=103)
        watcher.feed(
            {"ts": 104, "author": "GM", "content": "Rain falls."}, now=104)

        self.assertEqual([event for event in events
                          if event["event"] == "open"], [])
        watcher.feed(
            {"ts": 105, "author": "GM", "content": "A bell rings."}, now=105)
        opened = [event for event in events if event["event"] == "open"]
        self.assertEqual(len(opened), 1)
        self.assertEqual(opened[0]["rule"], "R4")

    def test_cue_anywhere_in_window_prevents_finding(self):
        ch = charter("R4", cue_within_gm_messages=3)
        transcript = indexed([
            {"ts": 101, "author": "GM", "content": "The door opens."},
            {"ts": 102, "author": "GM", "content": "Ash, your move."},
            {"ts": 103, "author": "GM", "content": "Rain falls."},
        ])
        ledger = [{"ts": 100, "type": "turn", "actor": "Ash"}]
        findings, code = check(transcript, ch, ledger, closed=True)
        self.assertEqual((findings, code), ([], 0))

    def test_close_with_short_window_is_incomplete_not_accusation(self):
        ch = charter("R4", cue_within_gm_messages=3)
        events = []
        watcher = Watcher(
            ch, [{"ts": 100, "type": "turn", "actor": "Ash"}],
            emit=events.append)
        watcher.feed(
            {"ts": 101, "author": "GM", "content": "The door opens."}, now=101)
        watcher.feed(
            {"ts": 102, "author": "GM", "content": "Rain falls."}, now=102)

        self.assertEqual(watcher.close(now=102), 0)
        terminal = events[-1]
        self.assertEqual(terminal["event"], "session_end")
        self.assertEqual(terminal["findings"], [])
        r4 = [item for item in terminal["incomplete"]
              if item.get("rule") == "R4"]
        self.assertEqual(len(r4), 1)
        self.assertEqual(r4[0]["observed_gm_beats"], 2)
        self.assertEqual(r4[0]["required_gm_beats"], 3)


class TestR7Lifecycle(TestCase):
    def test_elapsed_detail_has_one_identity_open_resolution_and_notification(self):
        ch = charter("R7", dead_air_seconds=300,
                     quiet_table_max_messages=3)
        events = []
        with mock.patch("dmcheck.watch.subprocess.run") as notify:
            watcher = Watcher(ch, emit=events.append,
                              notify_cmd="notify-dmcheck")
            watcher.feed(
                {"ts": 0, "author": "Ash", "content": "I open it."}, now=0)
            watcher.tick(now=301)
            first_detail = next(iter(watcher.open.values()))["detail"]
            watcher.tick(now=302)
            watcher.tick(now=303)
            refreshed_detail = next(iter(watcher.open.values()))["detail"]

            # Three explicitly-OOC table beats prove that the GM yielded the
            # floor; they are not themselves R7 candidates.
            for ts in (100, 200, 300):
                watcher.feed(
                    {"ts": ts, "author": "Table",
                     "content": "[OOC] still planning"}, now=303)
            watcher.tick(now=304)

        opened = [event for event in events if event["event"] == "open"]
        resolved = [event for event in events if event["event"] == "resolved"]
        self.assertEqual(len(opened), 1)
        self.assertEqual(len(resolved), 1)
        self.assertEqual(opened[0]["finding_id"], resolved[0]["finding_id"])
        self.assertNotEqual(first_detail, refreshed_detail)
        self.assertEqual(notify.call_count, 1)

    def test_id_is_stable_when_elapsed_text_changes(self):
        ch = charter("R7", dead_air_seconds=300)
        transcript = indexed([
            {"ts": 0, "author": "Ash", "content": "I open it."},
        ])
        at_301, _ = check(transcript, ch, closed=False, now=301)
        at_999, _ = check(transcript, ch, closed=False, now=999)
        self.assertNotEqual(at_301[0]["detail"], at_999[0]["detail"])
        self.assertEqual(at_301[0]["finding_id"], at_999[0]["finding_id"])
        self.assertEqual(at_301[0]["evidence"], at_999[0]["evidence"])

    def test_terminal_unresolved_resolved_and_insufficient_states(self):
        ch = charter("R7", dead_air_seconds=300)

        unresolved_events = []
        unresolved = Watcher(ch, emit=unresolved_events.append)
        unresolved.feed(
            {"ts": 0, "author": "Ash", "content": "I open it."}, now=0)
        unresolved.tick(now=301)
        self.assertEqual(unresolved.close(now=302), 1)
        unresolved_end = unresolved_events[-1]
        self.assertEqual(unresolved_end["open_count"], 1)
        self.assertIn("session close", unresolved_end["findings"][0]["detail"])
        self.assertEqual(unresolved_end["incomplete"], [])

        insufficient_events = []
        insufficient = Watcher(ch, emit=insufficient_events.append)
        insufficient.feed(
            {"ts": 100, "author": "Ash", "content": "I open it."}, now=100)
        self.assertEqual(insufficient.close(now=200), 0)
        insufficient_end = insufficient_events[-1]
        self.assertEqual(insufficient_end["findings"], [])
        self.assertEqual(insufficient_end["status"], "incomplete")
        self.assertEqual(insufficient_end["incomplete"][0]["rule"], "R7")

        resolved_events = []
        resolved = Watcher(ch, emit=resolved_events.append)
        resolved.ingest(messages=(
            {"ts": 0, "author": "Ash", "content": "I open it."},
            {"ts": 100, "author": "GM", "content": "It opens."},
        ), now=100)
        self.assertEqual(resolved.close(now=100), 0)
        resolved_end = resolved_events[-1]
        self.assertEqual(resolved_end["status"], "complete")
        self.assertEqual(resolved_end["findings"], [])
        self.assertEqual(resolved_end["incomplete"], [])


class TestTerminalInvariant(TestCase):
    def test_closed_watch_equals_batch_exact_findings_ids_count_and_evidence(self):
        ch = load_charter(str(FIX / "charter.json"))
        transcript = load_transcript(str(FIX / "messy-session.jsonl"))
        ledger = load_ledger(str(FIX / "messy-ledger.jsonl"))
        horizon = transcript[-1]["ts"]
        expected, expected_code = check(
            transcript, ch, ledger, closed=True, now=horizon)

        events = []
        watcher = Watcher(ch, ledger, emit=events.append)
        for message in transcript:
            watcher.feed(message, now=message["ts"])
        self.assertEqual(watcher.close(now=horizon), expected_code)

        terminal = events[-1]
        self.assertEqual(terminal["findings"], expected)
        self.assertEqual(terminal["finding_ids"],
                         [finding["finding_id"] for finding in expected])
        self.assertEqual(terminal["open_count"], len(expected))
        self.assertEqual(
            [finding["evidence"] for finding in terminal["findings"]],
            [finding["evidence"] for finding in expected])

    def test_duplicate_source_delivery_is_idempotent(self):
        ch = charter("R6")
        ch["hidden_terms"] = ["secret"]
        row = {"ts": 10, "author": "GM", "content": "the secret leaks"}
        events = []
        watcher = Watcher(ch, emit=events.append)
        watcher.feed(row, now=10)
        watcher.feed(dict(row), now=11)
        watcher.close(now=11)

        opened = [event for event in events if event["event"] == "open"]
        self.assertEqual(len(opened), 1)
        self.assertEqual(len(watcher.messages), 1)
        first, _ = check(indexed([row]), ch)
        second, _ = check(indexed([row]), ch)
        self.assertEqual(first, second)
        self.assertEqual(events[-1]["findings"], first)

    def test_distinct_ledger_ids_do_not_collapse_identical_payloads(self):
        ch = charter("R3")
        transcript = indexed([
            {"ts": 1, "author": "GM", "content": "All is calm."},
        ])
        first = {"id": "evt-1", "ts": 2, "type": "event", "text": "hit"}
        second = {"id": "evt-2", "ts": 2, "type": "event", "text": "hit"}
        findings, _ = check(transcript, ch, [first, dict(first), second])
        self.assertEqual(len(findings), 2)
        self.assertEqual(len({finding["finding_id"] for finding in findings}), 2)
        self.assertNotEqual(findings[0]["evidence"]["source_fingerprint"],
                            findings[1]["evidence"]["source_fingerprint"])

    def test_iso_timestamp_and_discord_author_normalize_in_watch(self):
        ch = charter("R7", dead_air_seconds=5)
        events = []
        watcher = Watcher(ch, emit=events.append)
        watcher.ingest(messages=(
            {"timestamp": "1970-01-01T00:00:00Z",
             "author": {"username": "Ash"}, "content": "I open it."},
            {"timestamp": "1970-01-01T00:00:10Z",
             "author": {"username": "GM"}, "content": "It opens."},
        ), now=10)
        opened = [event for event in events if event["event"] == "open"]
        self.assertEqual(len(opened), 1)
        self.assertEqual(opened[0]["rule"], "R7")
        self.assertEqual(opened[0]["evidence"]["ts"], 0.0)

    def test_pause_resume_and_idempotent_session_end_are_explicit(self):
        ch = charter("R7", dead_air_seconds=10)
        events = []
        watcher = Watcher(ch, emit=events.append)
        watcher.feed(
            {"ts": 0, "author": "Ash", "content": "I open it."}, now=0)
        watcher.pause()
        watcher.tick(now=20)
        self.assertFalse(any(event["event"] == "open" for event in events))
        watcher.resume(now=20)
        self.assertTrue(any(event["event"] == "open" for event in events))
        self.assertEqual(watcher.close(now=21), 1)
        self.assertEqual(watcher.close(now=99), 1)
        self.assertEqual(
            [event["event"] for event in events].count("session_paused"), 1)
        self.assertEqual(
            [event["event"] for event in events].count("session_resumed"), 1)
        self.assertEqual(
            [event["event"] for event in events].count("session_end"), 1)

    def test_seeded_batch_watch_property_is_exact_and_lifecycle_is_bounded(self):
        rng = random.Random(23002)
        ch = charter(
            "R1", "R2", "R3", "R4", "R5", "R6", "R7", "R8",
            answer_within_messages=3, roll_ack_within_messages=2,
            cue_within_gm_messages=2, dead_air_seconds=30,
            quiet_table_max_messages=3)
        ch["hidden_terms"] = ["spoiler"]

        for case in range(64):
            timestamp = 1000.0
            transcript = []
            authors = ("GM", "Ash", "Bram", "DiceBot")
            phrases = ("the door opens", "GM, what DC?", "I wait",
                       "spoiler stirs", "1d20=12", "[OOC] planning")
            for index in range(rng.randint(1, 14)):
                timestamp += float(rng.randint(1, 45))
                author = rng.choice(authors)
                content = rng.choice(phrases) + " #" + str(index)
                transcript.append({"i": index, "ts": timestamp,
                                   "author": author, "content": content})

            ledger = []
            for index in range(rng.randint(0, 7)):
                event_type = rng.choice(("turn", "act", "event"))
                event = {
                    "id": "case-{}-event-{}".format(case, index),
                    "ts": 1000.0 + float(rng.randint(0, 600)),
                    "type": event_type,
                    "text": "engine event {}".format(index),
                }
                if event_type in ("turn", "act"):
                    event["actor"] = rng.choice(("Ash", "Bram"))
                ledger.append(event)
            ledger.sort(key=lambda event: event["ts"])
            horizon = transcript[-1]["ts"] + float(rng.choice((0, 31, 90)))

            expected, expected_code = check(
                transcript, ch, ledger, closed=True, now=horizon)
            events = []
            watcher = Watcher(ch, ledger, emit=events.append)
            for message in transcript:
                watcher.feed(message, now=message["ts"])
            actual_code = watcher.close(now=horizon)
            terminal = events[-1]

            with self.subTest(case=case):
                self.assertEqual(actual_code, expected_code)
                self.assertEqual(terminal["findings"], expected)
                self.assertEqual(terminal["open_count"], len(expected))
                opens = [event["finding_id"] for event in events
                         if event["event"] == "open"]
                resolutions = [event["finding_id"] for event in events
                               if event["event"] == "resolved"]
                self.assertEqual(len(opens), len(set(opens)))
                self.assertEqual(len(resolutions), len(set(resolutions)))

    def test_cli_can_replay_the_terminal_evaluation_horizon_exactly(self):
        from tempfile import TemporaryDirectory
        from dmcheck.cli import main

        ch = charter("R7", dead_air_seconds=300)
        events = []
        watcher = Watcher(ch, emit=events.append)
        row = {"ts": 0, "author": "Ash", "content": "I open it."}
        watcher.feed(row, now=0)
        watcher.tick(now=301)
        watcher.close(now=302)
        terminal = events[-1]

        with TemporaryDirectory() as directory:
            root = Path(directory)
            transcript_path = root / "transcript.jsonl"
            charter_path = root / "charter.json"
            transcript_path.write_text(jsonl(row), encoding="utf-8")
            charter_path.write_text(json.dumps(ch), encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main([
                    "run", str(transcript_path), "--charter", str(charter_path),
                    "--evaluation-ts", str(terminal["evaluation_ts"]),
                ])
            replay = json.loads(output.getvalue())

        self.assertEqual(code, 1)
        self.assertEqual(replay["evaluation_ts"], terminal["evaluation_ts"])
        self.assertEqual(replay["findings"], terminal["findings"])


class TestJsonlFollower(TestCase):
    def test_partial_line_duplicate_and_cursor_restart(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            path = Path(directory) / "transcript.jsonl"
            first = {"ts": 1, "author": "Ash", "content": "one"}
            second = {"ts": 2, "author": "Ash", "content": "two"}
            path.write_text(jsonl(first), encoding="utf-8")
            health = []
            follower = JsonlFollower(
                str(path), "transcript", emit=health.append)
            self.assertEqual(len(follower.poll()), 1)

            encoded = jsonl(second)
            split = len(encoded) // 2
            with path.open("a", encoding="utf-8") as handle:
                handle.write(encoded[:split])
            self.assertEqual(follower.poll(), [])
            self.assertEqual(follower.coverage, "pending")

            cursor = follower.cursor()
            restarted = JsonlFollower(
                str(path), "transcript", emit=health.append, cursor=cursor)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(encoded[split:])
            rows = restarted.poll()
            self.assertEqual([row["content"] for row in rows], ["two"])
            self.assertEqual(restarted.coverage, "complete")

            with path.open("a", encoding="utf-8") as handle:
                handle.write(jsonl(second))
            self.assertEqual(restarted.poll(), [])
            reasons = [event["reason"] for event in health]
            self.assertIn("partial_line", reasons)
            self.assertIn("partial_line_committed", reasons)
            self.assertIn("duplicate_delivery_ignored", reasons)

            final_cursor = restarted.cursor()
            second_restart = JsonlFollower(
                str(path), "transcript", cursor=final_cursor)
            self.assertEqual(second_restart.poll(), [])
            third = {"ts": 3, "author": "Ash", "content": "three"}
            with path.open("a", encoding="utf-8") as handle:
                handle.write(jsonl(third))
            self.assertEqual(
                [row["content"] for row in second_restart.poll()], ["three"])

    def test_truncation_rotation_and_same_size_rewrite_are_detected(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            root = Path(directory)

            truncated_path = root / "truncated.jsonl"
            truncated_path.write_text(jsonl(
                {"ts": 10, "author": "Ash", "content": "x" * 500}),
                encoding="utf-8")
            truncated_health = []
            truncated = JsonlFollower(
                str(truncated_path), "transcript",
                emit=truncated_health.append)
            truncated.poll()
            truncated_path.write_text(jsonl(
                {"ts": 11, "author": "Ash", "content": "new"}),
                encoding="utf-8")
            self.assertEqual(
                [row["content"] for row in truncated.poll()], ["new"])
            self.assertEqual(truncated.coverage, "incomplete")
            self.assertIn("truncation_detected",
                          [event["reason"] for event in truncated_health])

            rewritten_path = root / "rewritten.jsonl"
            old_row = {"ts": 20, "author": "Ash", "content": "AAAA"}
            new_row = {"ts": 21, "author": "Ash", "content": "BBBB"}
            self.assertEqual(len(jsonl(old_row)), len(jsonl(new_row)))
            rewritten_path.write_text(jsonl(old_row), encoding="utf-8")
            rewritten_health = []
            rewritten = JsonlFollower(
                str(rewritten_path), "transcript", emit=rewritten_health.append)
            rewritten.poll()
            rewritten_path.write_text(jsonl(new_row), encoding="utf-8")
            self.assertEqual(
                [row["content"] for row in rewritten.poll()], ["BBBB"])
            self.assertEqual(rewritten.coverage, "incomplete")
            self.assertIn("rewrite_detected",
                          [event["reason"] for event in rewritten_health])

            rotated_path = root / "rotated.jsonl"
            rotated_path.write_text(jsonl(old_row), encoding="utf-8")
            rotated_health = []
            rotated = JsonlFollower(
                str(rotated_path), "transcript", emit=rotated_health.append)
            rotated.poll()
            os.replace(str(rotated_path), str(root / "rotated.old"))
            rotated_path.write_text(jsonl(new_row), encoding="utf-8")
            self.assertEqual(
                [row["content"] for row in rotated.poll()], ["BBBB"])
            self.assertEqual(rotated.coverage, "incomplete")
            self.assertIn("rotation_detected",
                          [event["reason"] for event in rotated_health])

    def test_out_of_order_and_invalid_complete_rows_degrade_coverage(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.jsonl"
            path.write_text(jsonl(
                {"ts": 20, "type": "event", "text": "later"}),
                encoding="utf-8")
            health = []
            follower = JsonlFollower(str(path), "ledger", emit=health.append)
            follower.poll()
            with path.open("a", encoding="utf-8") as handle:
                handle.write(jsonl(
                    {"ts": 10, "type": "event", "text": "earlier"}))
                handle.write("{not-json}\n")
            rows = follower.poll()
            self.assertEqual([row["text"] for row in rows], ["earlier"])
            self.assertEqual(follower.coverage, "incomplete")
            reasons = [event["reason"] for event in health]
            self.assertIn("out_of_order_row", reasons)
            self.assertIn("invalid_complete_row", reasons)

    def test_partial_source_is_terminal_incomplete_not_a_finding(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            path = Path(directory) / "transcript.jsonl"
            path.write_text('{"ts":1,"author":"Ash"', encoding="utf-8")
            events = []
            watcher = Watcher(charter("R4"), emit=events.append)
            follower = JsonlFollower(
                str(path), "transcript", emit=watcher.record_health)
            poll_sources(watcher, transcript_follower=follower)
            self.assertEqual(watcher.close(), 0)
            terminal = events[-1]
            self.assertEqual(terminal["findings"], [])
            self.assertEqual(terminal["coverage"]["transcript"], "pending")
            self.assertEqual(terminal["status"], "incomplete")


class TestFollowEndToEnd(TestCase):
    def test_transcript_and_ledger_appends_are_followed_after_startup(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            root = Path(directory)
            transcript_path = root / "transcript.jsonl"
            ledger_path = root / "ledger.jsonl"
            charter_path = root / "charter.json"
            transcript_path.write_text(jsonl(
                {"ts": 100, "author": "GM", "content": "All is calm."}),
                encoding="utf-8")
            ledger_path.write_text("", encoding="utf-8")
            charter_path.write_text(json.dumps(charter("R3")),
                                    encoding="utf-8")

            calls = {"count": 0}

            def mutate_sources(_interval):
                calls["count"] += 1
                if calls["count"] == 1:
                    with ledger_path.open("a", encoding="utf-8") as handle:
                        handle.write(jsonl(
                            {"ts": 200, "type": "event",
                             "text": "the ward breaks"}))
                elif calls["count"] == 2:
                    with transcript_path.open("a", encoding="utf-8") as handle:
                        handle.write(jsonl(
                            {"ts": 300, "author": "GM",
                             "content": "The ward breaks."}))
                else:
                    raise KeyboardInterrupt

            args = SimpleNamespace(
                charter=str(charter_path), gm=None, dice_bot=None,
                transcript=str(transcript_path), ledger=str(ledger_path),
                follow=True, interval=0, notify_cmd=None, craft=False,
                scene="SOCIAL", pc=None)
            output = io.StringIO()
            with mock.patch("dmcheck.watch.time.sleep",
                            side_effect=mutate_sources), \
                    mock.patch("dmcheck.watch.time.time", return_value=400), \
                    contextlib.redirect_stdout(output):
                code = watch_main(args)

            events = [json.loads(line) for line in output.getvalue().splitlines()]
            opened = [event for event in events
                      if event["event"] == "open" and event["rule"] == "R3"]
            resolved = [event for event in events
                        if event["event"] == "resolved" and event["rule"] == "R3"]
            self.assertEqual(code, 0)
            self.assertEqual(len(opened), 1)
            self.assertEqual(len(resolved), 1)
            self.assertEqual(opened[0]["finding_id"],
                             resolved[0]["finding_id"])
            terminal = events[-1]
            self.assertEqual(terminal["findings"], [])
            self.assertEqual(terminal["evaluation_ts"], 400)
            self.assertEqual(terminal["coverage"],
                             {"ledger": "complete", "transcript": "complete"})
            self.assertEqual(terminal["status"], "complete")
