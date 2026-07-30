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


def test_watch_emits_advisory_craft_events():
    from dmcheck.watch import Watcher
    ev = []
    ch = {"gm": ["GM"], "dice_authors": [], "ooc_markers": [], "hidden_terms": [],
          "thresholds": {}, "rules_enabled": [], "seats": {}}
    w = Watcher(ch, craft=True, pcs=("Teodor",), emit=ev.append)
    for k in range(4):
        w.feed({"ts": k, "author": "GM", "content": "word " * 60}, now=k)
    kinds = {e["event"] for e in ev}
    assert "craft_attention" in kinds
    assert all(e.get("advisory") for e in ev if e["event"].startswith("craft"))
    n = sum(1 for e in ev if e["event"] == "craft_attention")
    assert n == 1          # resolve-and-move-on: same notice never re-emitted


def test_seat_quiet_advisory():
    from dmcheck.watch import Watcher
    ev = []
    ch = {"gm": ["GM"], "dice_authors": [], "ooc_markers": [], "hidden_terms": [],
          "thresholds": {}, "rules_enabled": [], "seats": {}}
    w = Watcher(ch, craft=True, pcs=("William", "Shalia"), emit=ev.append)
    w.feed({"ts": 0, "author": "William", "content": "I carry the breastplate"}, now=0)
    for k in range(4):
        w.feed({"ts": k + 1, "author": "Shalia", "content": "I act"}, now=k + 1)
        w.feed({"ts": k + 1.5, "author": "GM", "content": "it happens " * 3}, now=k + 1.5)
    quiet = [e for e in ev if e["event"] == "seat_quiet"]
    assert quiet and quiet[0]["seat"] == "William"
    assert all(e["advisory"] for e in quiet)
    assert not any(e["seat"] == "Shalia" for e in quiet)


def test_rule_checks_1a_1b_11a_11b():
    from dmcheck.craft import rule_checks
    beats = [
        "The goblin lunges - roll initiative!",                     # onset, no order posted
        "It hits you for 8 damage and steps back behind the barrel then circles wide looking for an opening while you recover",  # long damage beat
        "Your turn.",                                               # turn-adv, no PC name
        "The goblin dies.",                                         # bare-number kill
    ]
    r = rule_checks(beats, ("Teodor",))
    rules = {f["rule"] for f in r["findings"]}
    assert {"1a", "1b", "11b"} <= rules
    clean = ["Steel rings - roll initiative! The order is Teodor, then the goblin.",
             "8 damage.", "That brings us to Teodor.",
             "Your blade takes it through the throat mid-leap and it crashes into the "
             "barrels, kicks once, and is dead before the wine stops spilling - "
             "how do you want to do this moment to end, Teodor? Describe the finish."]
    r2 = rule_checks(clean, ("Teodor",))
    assert not [f for f in r2["findings"] if f["rule"] in ("1a", "1b")]
