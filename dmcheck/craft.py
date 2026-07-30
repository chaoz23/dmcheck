"""dmcheck craft - the attention lane (v0.5). Statistics and categorical
defects, NEVER a score. Envelope measured over 134h of two professional DMs;
categorical detectors born from a live second-agent test (2026-07-27) where
the rate meter was structurally blind to all of them. Advisory only."""
import json
import re
import statistics as st

ENV = {"median_words": (14, 17), "long40": (.12, .19), "question": (.175, .20),
       "roll": (.05, .09), "npc_voice": (.16, .22)}
SALIENCE = {"COMBAT": {"median_words": 1.3, "roll": 1.2, "question": .6,
                       "npc_voice": .3, "long40": 1.0},
            "SOCIAL": {"median_words": 1.0, "roll": 1.3, "question": 1.3,
                       "npc_voice": 1.0, "long40": 1.0},
            "EXPLORATION": {"median_words": 1.0, "roll": .9, "question": 1.1,
                            "npc_voice": .7, "long40": 1.2}}
ROLL = re.compile(r"\b(roll|make a|give me a)\b", re.I)
NOROLL = re.compile(r"\b(no|without|don't|do not|need|needed)\s+(a\s+)?rolls?\b", re.I)


def rates(beats):
    w = [len(b.split()) for b in beats]
    n = len(beats) or 1
    return {"median_words": st.median(w) if w else 0,
            "long40": sum(1 for x in w if x > 40) / n,
            "question": sum(1 for b in beats if "?" in b) / n,
            "roll": sum(1 for b in beats
                        if ROLL.search(b) and not NOROLL.search(b)) / n,
            "npc_voice": sum(1 for b in beats if '"' in b) / n}


def attention(beats, scene="SOCIAL", window=5, resolved=()):
    """ONE signal - the most out-of-band metric for this scene type - or None.
    A dashboard is not attention: five dials cost the author the metric he
    was not watching."""
    b = beats[-window:]
    if len(b) < 3:
        return None
    got = rates(b)
    sal = SALIENCE.get(scene.upper(), SALIENCE["SOCIAL"])
    worst, score = None, 0.0
    for k, (lo, hi) in ENV.items():
        if k in resolved:
            continue
        v = got[k]
        if lo <= v <= hi:
            continue
        dev = ((lo - v) if v < lo else (v - hi)) / ((hi - lo) or 1)
        if dev * sal[k] > score:
            worst, score = k, dev * sal[k]
    if not worst:
        return None
    v, (lo, hi) = got[worst], ENV[worst]
    return {"notice": worst, "value": round(v, 2), "band": [lo, hi],
            "direction": "less" if v > hi else "more", "scene": scene.upper()}


def hard_defects(beat, pc_names=()):
    """Categorical failures a rate meter cannot see. Attribution rule: a
    quoted line in a DM beat belongs to an NPC unless a PC is implicated."""
    out = []
    for m in re.finditer(r'"[^"]{3,}"', beat):
        pre = beat[max(0, m.start() - 100):m.start()]
        for pc in pc_names:
            if re.search(r"\b" + re.escape(pc) + r"\b[^\"]*$", pre, re.I):
                out.append(f"voiced_the_pc:{pc}")
    if re.search(r"\bif\b[^.]{0,60}\b(then|,)[^.]{0,60}\b(if|otherwise)\b", beat, re.I):
        out.append("exposed_inference_tree")
    if re.search(r"\b\w+(?: \w+)?\s+or\s+\w+(?: \w+)?,?\s*(whichever|either)\b", beat, re.I):
        out.append("deferred_the_adjudication")
    if pc_names and re.search(r"\bi'?ll roll\b", beat, re.I):
        out.append("rolled_for_the_player")
    return sorted(set(out))




# --- rule checks (v0.5.4): protocol rules 1a/1b/11a/11b, made machine-run ---
# Per the testability charter (rule 70) these are the rules whose falsification
# tests are statable from a transcript alone. Advisory findings, never a score;
# kill detection is lexical, so 11b findings carry that caveat explicitly.
INITIATIVE = re.compile(r"\broll initiative\b", re.I)
ORDER_POST = re.compile(r"\b(first up|top of the (?:round|order)|the order is|"
                        r"goes first|order:)\b", re.I)
TURN_ADV = re.compile(r"\b(your turn|next up|that brings us to|you'?re up|"
                      r"next (?:is|to act))\b", re.I)
DMG_N = re.compile(r"\b\d{1,3} (?:points? of )?\w* ?damage\b", re.I)
KILL = re.compile(r"\b(dies|is dead|drops? dead|slain|breathes? (?:his|her|its) last|"
                  r"down for good|kills? (?:it|him|her|them)|finished (?:it|him|her)|"
                  r"how do you want to do this)\b", re.I)
COMBAT_SPAN = 30   # GM beats per combat window, or until the next onset


def rule_checks(beats, pc_names=()):
    onsets = [i for i, b in enumerate(beats) if INITIATIVE.search(b)]
    findings = []
    windows = []
    for k, i in enumerate(onsets):
        end = onsets[k + 1] if k + 1 < len(onsets) else min(i + COMBAT_SPAN, len(beats))
        win = beats[i:end]
        windows.append((i, win))
        # 1a: full order posted within 5 GM beats of onset
        if not any(ORDER_POST.search(b) for b in win[:6]):
            findings.append({"rule": "1a", "beat": i,
                             "fail": "no initiative order posted within 5 GM beats of onset"})
        # 1b: turn-advance beats name a PC (>=95% of them)
        if pc_names:
            adv = [b for b in win if TURN_ADV.search(b)]
            named = [b for b in adv if any(re.search(r"\b" + re.escape(p) + r"\b", b, re.I)
                                           for p in pc_names)]
            if adv and len(named) / len(adv) < 0.95:
                findings.append({"rule": "1b", "beat": i,
                                 "fail": f"only {len(named)}/{len(adv)} turn-advance "
                                         f"beats name a PC (test: >=95%)"})
    # 11a: combat damage beats median under 20 words
    dmg = [b for _, win in windows for b in win if DMG_N.search(b)]
    if dmg:
        med = st.median(len(b.split()) for b in dmg)
        if med >= 20:
            findings.append({"rule": "11a", "beat": None,
                             "fail": f"combat damage-beat median {med:.0f} words "
                                     f"(test: <20 - numbers are the register at pace)"})
    # 11b: kill beats get ceremony (>=3x combat median) - lexical kill
    # detection, so each finding is a REVIEW ITEM, not a verdict
    for i, win in windows:
        wmed = st.median(len(b.split()) for b in win) if win else 0
        for j, b in enumerate(win):
            if KILL.search(b) and wmed and len(b.split()) < 3 * wmed                     and "how do you want" not in b.lower():
                findings.append({"rule": "11b", "beat": i + j,
                                 "fail": f"possible bare-number kill ({len(b.split())}w "
                                         f"vs 3x median {3*wmed:.0f}w) - lexical kill "
                                         f"detection, review before trusting"})
    return {"combat_onsets": len(onsets), "damage_beats": len(dmg),
            "findings": findings,
            "contract": "advisory rule checks (protocol 1a/1b/11a/11b) - review items, no score"}


def report(beats, scene="SOCIAL", pc_names=()):
    hd = [{"beat": i, "defects": d} for i, b in enumerate(beats)
          if (d := hard_defects(b, pc_names))]
    return {"beats": len(beats), "rates": {k: round(v, 3) for k, v in rates(beats).items()},
            "envelope": ENV, "attention": attention(beats, scene),
            "hard_defects": hd,
            "rule_checks": rule_checks(beats, pc_names),
            "contract": "advisory statistics only; nothing here aggregates into a single number"}
