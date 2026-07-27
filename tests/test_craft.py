from dmcheck.craft import attention, hard_defects, report


def test_attention_one_signal_and_resolve():
    beats = ["word " * 60] * 5
    a = attention(beats, "SOCIAL")
    assert a["notice"] == "median_words" and a["direction"] == "less"
    a2 = attention(beats, "SOCIAL", resolved=("median_words", "long40"))
    assert a2 is None or a2["notice"] not in ("median_words", "long40")


def test_hard_defects_catch_boundaries():
    d = hard_defects('Teodor keeps his voice low. "Camels don\'t lie." '
                     "I'll roll Survival or Animal Handling, whichever fits.",
                     ("Teodor",))
    assert "voiced_the_pc:Teodor" in d
    assert "deferred_the_adjudication" in d
    assert "rolled_for_the_player" in d


def test_report_has_no_score():
    r = report(["a short beat"] * 4)
    assert "score" not in r  # no aggregate key, ever


def json_str(x):
    import json
    return json.dumps(x)
