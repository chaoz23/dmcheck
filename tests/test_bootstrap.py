"""v0.3: init bootstrap + per-seat cue policy (R4 false-pass fix).
Origin: live table 2026-07-24 — 'Shalia — you're up' in prose never reached
the agent seat (allowBots='mentions' drops unmentioned bot messages); R4
passed on a cue that was undeliverable."""
import json
from dmcheck.cli import main
from dmcheck.core import check


def _rows(rows):
    for i, r in enumerate(rows):
        r["i"] = i
    return rows


CH = {"gm": ["OCE"], "dice_authors": [], "ooc_markers": [], "hidden_terms": [],
      "thresholds": {}, "rules_enabled": ["R4"],
      "seats": {"Shalia": {"cue_requires_mention": True, "mention": "<@42>"}}}


def test_init_charter_passes_lint(tmp_path, capsys):
    out = tmp_path / "charter.json"
    assert main(["init", str(out), "--gm", "OCE"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["session_zero"]["S3c"].startswith("bootstrap sheet accountability")
    assert main(["lint-charter", str(out)]) == 0
    ch = json.load(open(out))
    assert ch["gm"] == ["OCE"] and ch["rules_enabled"][0] == "R1"


def test_r4_name_in_prose_is_not_a_cue_for_mention_gated_seat():
    ledger = [{"ts": 100.0, "type": "turn", "actor": "Shalia"}]
    T = _rows([{"ts": 101.0, "author": "OCE",
                "content": "Shalia — you're up!"}])   # undeliverable
    findings, code = check(T, dict(CH), ledger)
    assert code == 1 and findings[0]["rule"] == "R4"
    assert "required mention" in findings[0]["detail"]


def test_r4_literal_mention_satisfies():
    ledger = [{"ts": 100.0, "type": "turn", "actor": "Shalia"}]
    T = _rows([{"ts": 101.0, "author": "OCE",
                "content": "<@42> Shalia — you're up!"}])
    findings, code = check(T, dict(CH), ledger)
    assert code == 0 and not findings


def test_r4_unconfigured_seat_falls_back_to_name():
    ch = dict(CH); ch["seats"] = {}
    ledger = [{"ts": 100.0, "type": "turn", "actor": "Shalia"}]
    T = _rows([{"ts": 101.0, "author": "OCE", "content": "Shalia — you're up!"}])
    findings, code = check(T, ch, ledger)
    assert code == 0


def test_lint_flags_mention_requirement_without_string(tmp_path):
    bad = dict(CH); bad["seats"] = {"Shalia": {"cue_requires_mention": True}}
    p = tmp_path / "c.json"; p.write_text(json.dumps(bad))
    assert main(["lint-charter", str(p)]) == 1
