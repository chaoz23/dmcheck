"""Conformance tests for the table-kit TableEvent v1 adapter."""

import json

from dmcheck import evaluate_table_event_path, load_table_events
from dmcheck.cli import main


def event(sequence, event_type, payload, *, actor="gm-dan", role="gm",
          audience=None, correlations=None, cause=None):
    stamp = "2026-08-01T19:%02d:00Z" % sequence
    return {
        "schema_version": "table.event/1.0",
        "event_id": "evt-%03d" % sequence,
        "campaign_id": "campaign-test",
        "session_id": "session-test",
        "session_sequence": sequence,
        "source": {"kind": "fixture", "instance": "dmcheck-tests",
                   "native_id": str(sequence), "sequence": sequence,
                   "attestation": "self_attested"},
        "occurred_at": stamp, "recorded_at": stamp,
        "principal": {"id": actor or "fixture", "actor_id": actor,
                      "controller_id": None, "role": role},
        "event_type": event_type, "payload": payload,
        "correlation_ids": correlations or [], "causation_id": cause,
        "audience": audience or ["table"], "visibility": "table",
        "sensitivity": "normal", "provenance": "observed",
        "integrity": {"predecessor_digest": None, "event_digest": None,
                      "checkpoint": "same_writer"},
    }


def write_events(tmp_path, events):
    path = tmp_path / "events.jsonl"
    path.write_text("".join(json.dumps(item) + "\n" for item in events),
                    encoding="utf-8")
    return path


def error_codes(result):
    return {item["code"] for item in result.to_dict()["errors"]}


def test_correlated_roll_narration_projects_cleanly(tmp_path):
    events = [
        event(1, "roll.observed", {"total": 19, "natural": 14,
                                   "roll_kind": "attack"},
              actor="fighter-vesh", role="agent", correlations=["roll-1"]),
        event(2, "narration.obligation",
              {"obligation_id": "obligation-roll-1", "kind": "consume_roll"},
              actor=None, role="system", audience=["gm-dan"], cause="evt-001"),
        event(3, "narration.observed",
              {"content": "The strike lands.",
               "resolves_obligation_ids": ["obligation-roll-1"]},
              correlations=["roll-1", "obligation-roll-1"], cause="evt-002"),
    ]
    path = write_events(tmp_path, events)
    projection = load_table_events(path)
    assert projection.transcript[0]["obligation_id"] == "obligation-roll-1"
    assert projection.transcript[1]["correlation_id"] == ["obligation-roll-1"]
    result = evaluate_table_event_path(path, gm=["gm-dan"])
    assert result.status == "clean"
    assert "R2" in result.eligible_rules


def test_unanswered_question_preserves_event_identity_in_finding(tmp_path):
    events = [
        event(1, "narration.observed",
              {"content": "You enter the hall.", "resolves_obligation_ids": []}),
        event(2, "message.observed",
              {"content": "Are the prisoners safe?", "content_redacted": False},
              actor="rogue-brae", role="player", audience=["gm-dan"],
              correlations=["question-1"]),
    ]
    result = evaluate_table_event_path(write_events(tmp_path, events),
                                       gm=["gm-dan"])
    assert result.status == "findings"
    r1 = next(item for item in result.findings if item["rule"] == "R1")
    assert r1["evidence"]["id"] == "evt-002"
    assert r1["evidence"]["correlation_id"] == ["question-1"]


def test_transport_gap_is_incomplete_never_false_clean(tmp_path):
    events = [
        event(1, "message.observed",
              {"content": "Ready?", "content_redacted": False},
              actor="rogue-brae", role="player", audience=["gm-dan"]),
        event(2, "transport.gap",
              {"expected_sequence": 7, "observed_sequence": 8,
               "recoverable": False}, actor=None, role="system",
              audience=["operator"]),
    ]
    result = evaluate_table_event_path(write_events(tmp_path, events),
                                       gm=["gm-dan"])
    assert result.status == "incomplete"
    assert result.exit_code == 2
    assert "table_event.transport_gap" in error_codes(result)


def test_redacted_message_is_incomplete_never_false_clean(tmp_path):
    item = event(1, "message.observed",
                 {"content": "", "content_redacted": True},
                 actor="rogue-brae", role="player")
    result = evaluate_table_event_path(write_events(tmp_path, [item]),
                                       gm=["gm-dan"])
    assert result.status == "incomplete"
    assert "table_event.content_redacted" in error_codes(result)


def test_unknown_schema_is_structured_incomplete(tmp_path):
    item = event(1, "message.observed",
                 {"content": "hello", "content_redacted": False})
    item["schema_version"] = "table.event/2.0"
    result = evaluate_table_event_path(write_events(tmp_path, [item]),
                                       gm=["gm-dan"])
    assert result.status == "incomplete"
    assert "table_event.unsupported_schema" in error_codes(result)


def test_unknown_event_type_is_structured_incomplete(tmp_path):
    item = event(1, "weather.changed", {"weather": "fog"})
    result = evaluate_table_event_path(write_events(tmp_path, [item]),
                                       gm=["gm-dan"])
    assert result.status == "incomplete"
    assert "table_event.unsupported_type" in error_codes(result)


def test_zero_compatible_stream_is_incomplete(tmp_path):
    item = event(1, "session.closed", {"reason": "completed"})
    result = evaluate_table_event_path(write_events(tmp_path, [item]),
                                       gm=["gm-dan"])
    assert result.status == "incomplete"
    assert "table_event.zero_compatible" in error_codes(result)


def test_sequence_gap_and_duplicate_are_invalid(tmp_path):
    first = event(1, "session.opened",
                  {"descriptor_digest": None, "authority_status": "self_attested"})
    second = event(3, "session.closed", {"reason": "completed"})
    second["event_id"] = first["event_id"]
    result = evaluate_table_event_path(write_events(tmp_path, [first, second]),
                                       gm=["gm-dan"])
    assert result.status == "invalid"
    assert {"table_event.sequence_gap", "table_event.duplicate_event"} <= error_codes(result)


def test_malformed_v1_payload_and_numeric_timestamp_are_invalid(tmp_path):
    item = event(1, "roll.observed", {"total": "nineteen", "roll_kind": ""})
    item["occurred_at"] = 123
    result = evaluate_table_event_path(write_events(tmp_path, [item]),
                                       gm=["gm-dan"])
    assert result.status == "invalid"
    assert {"table_event.number", "table_event.id",
            "table_event.timestamp_type"} <= error_codes(result)


def test_multiple_consume_obligations_are_incomplete(tmp_path):
    events = [
        event(1, "roll.observed", {"total": 19, "roll_kind": "attack"},
              actor="fighter-vesh", role="agent"),
        event(2, "narration.obligation", {"obligation_id": "consume-a",
                                          "kind": "consume_roll"},
              actor=None, role="system", cause="evt-001"),
        event(3, "narration.obligation", {"obligation_id": "consume-b",
                                          "kind": "consume_roll"},
              actor=None, role="system", cause="evt-001"),
        event(4, "narration.observed", {"content": "It lands.",
                                        "resolves_obligation_ids": ["consume-a"]}),
    ]
    result = evaluate_table_event_path(write_events(tmp_path, events),
                                       gm=["gm-dan"])
    assert result.status == "incomplete"
    assert "table_event.ambiguous_roll_obligation" in error_codes(result)


def test_legal_reaction_is_not_projected_as_authoritative_act(tmp_path):
    events = [
        event(1, "action.declared",
              {"action_kind": "shield", "legal_timing": "reaction"},
              actor="wizard-rowan", role="player"),
        event(2, "narration.observed",
              {"content": "The spell flares.", "resolves_obligation_ids": []}),
    ]
    projection = load_table_events(write_events(tmp_path, events))
    assert projection.ledger == []
    assert "action.declared" in projection.skipped_event_types
    result = evaluate_table_event_path(write_events(tmp_path, events),
                                       gm=["gm-dan"])
    assert not any(item["rule"] in {"R3", "R5"} for item in result.findings)


def test_run_events_cli_returns_typed_exit_code(tmp_path, capsys):
    path = write_events(tmp_path, [
        event(1, "transport.gap",
              {"expected_sequence": 1, "observed_sequence": 2,
               "recoverable": False}, actor=None, role="system",
              audience=["operator"]),
    ])
    assert main(["run-events", str(path), "--gm", "gm-dan"]) == 2
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "incomplete"


def test_run_events_rejects_separate_ledger(tmp_path, capsys):
    path = write_events(tmp_path, [
        event(1, "session.closed", {"reason": "completed"}),
    ])
    assert main(["run-events", str(path), "--gm", "gm-dan",
                 "--ledger", "events.jsonl"]) == 2
    output = json.loads(capsys.readouterr().out)
    assert output["errors"][0]["code"] == "table_event.ledger_conflict"
