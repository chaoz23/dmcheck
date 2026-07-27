"""v0.4 evidence bars, calibrated against 134h of professional play where
naive R1 fired 85x/episode (0 valid) and naive R7 flagged 115 gaps (~5 real)."""
from dmcheck.core import check


def _rows(rows):
    for i, r in enumerate(rows):
        r["i"] = i
    return rows


def CH(**kw):
    d = {"gm": ["GM"], "dice_authors": [], "ooc_markers": ["[OOC]"],
         "hidden_terms": [], "thresholds": {}, "rules_enabled": ["R1", "R7"],
         "seats": {}}
    d.update(kw)
    return d


def test_r1_banter_question_never_fires():
    T = _rows([{"ts": 1, "author": "A", "content": "It's a real poster?"},
               {"ts": 2, "author": "B", "content": "ha"},
               {"ts": 3, "author": "A", "content": "wild"},
               {"ts": 4, "author": "B", "content": "yeah"},
               {"ts": 5, "author": "A", "content": "ok"},
               {"ts": 6, "author": "B", "content": "mm"},
               {"ts": 7, "author": "A", "content": "right"}])
    f, code = check(T, CH(rules_enabled=["R1"]))
    assert code == 0


def test_r1_directed_question_with_waiting_table_fires():
    T = _rows([{"ts": 1, "author": "A", "content": "GM, what DC do I need?"}])
    f, code = check(T, CH(rules_enabled=["R1"]), closed=True)
    assert code == 0 or f == []  # empty window -> no accusation
    T2 = _rows([{"ts": 1, "author": "A", "content": "GM, what DC do I need?"},
                {"ts": 200, "author": "A", "content": "...anyone?"}])
    f2, c2 = check(T2, CH(rules_enabled=["R1"]))
    assert c2 == 1 and f2[0]["rule"] == "R1"


def test_r1_busy_table_suppresses():
    T = _rows([{"ts": 1, "author": "A", "content": "GM, what DC do I need?"},
               {"ts": 2, "author": "B", "content": "while he checks - I loot"},
               {"ts": 3, "author": "B", "content": "and I count the arrows"}])
    f, code = check(T, CH(rules_enabled=["R1"]))
    assert code == 0


def test_r7_yielded_floor_exempt():
    T = _rows([{"ts": 0, "author": "GM", "content": "the door opens"},
               {"ts": 10, "author": "A", "content": "we should plan"},
               {"ts": 100, "author": "B", "content": "agreed, listen"},
               {"ts": 200, "author": "A", "content": "here's the idea"},
               {"ts": 300, "author": "B", "content": "good"},
               {"ts": 400, "author": "GM", "content": "as you finish planning..."}])
    f, code = check(T, CH(rules_enabled=["R7"]))
    assert code == 0            # 390s gap but the table was ACTIVE


def test_r7_genuine_silence_fires():
    T = _rows([{"ts": 0, "author": "GM", "content": "the door opens"},
               {"ts": 10, "author": "A", "content": "I open it"},
               {"ts": 400, "author": "GM", "content": "sorry, back"}])
    f, code = check(T, CH(rules_enabled=["R7"]))
    assert code == 1 and f[0]["rule"] == "R7"


def test_r4_alias_cue_counts():
    ch = CH(rules_enabled=["R4"],
            seats={"Ash": {"aliases": ["Shalia"]}})
    ledger = [{"ts": 100.0, "type": "turn", "actor": "Ash"}]
    T = _rows([{"ts": 101.0, "author": "GM",
                "content": "Shalia - the rope is fraying. Your move."}])
    f, code = check(T, ch, ledger)
    assert code == 0            # character-name cue satisfies via alias
