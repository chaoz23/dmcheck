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


def report(beats, scene="SOCIAL", pc_names=()):
    hd = [{"beat": i, "defects": d} for i, b in enumerate(beats)
          if (d := hard_defects(b, pc_names))]
    return {"beats": len(beats), "rates": {k: round(v, 3) for k, v in rates(beats).items()},
            "envelope": ENV, "attention": attention(beats, scene),
            "hard_defects": hd,
            "contract": "advisory statistics only; nothing here aggregates into a single number"}
