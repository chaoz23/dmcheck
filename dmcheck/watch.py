"""dmcheck live mode — the referee sits AT the table (v0.2).

Same engine as post-hoc `run`; a Watcher evaluates the growing transcript on
every message and every tick, diffs the finding set, and emits lifecycle
events: OPEN (violation just became provable — thresholds fully elapsed, no
predictions) and RESOLVED (a living condition healed, e.g. an engine event got
narrated). At session end a closed-mode evaluation runs, so the final OPEN set
provably equals `dmcheck run` on the full transcript.
"""
import json
import subprocess
import sys
import time

from .core import evaluate, invalid_result, load_charter, load_ledger, load_transcript
from .core import redact_output
from .validation import (InputValidationError, issue, normalize_charter,
                         normalize_ledger,
                         normalize_transcript, parse_json_value)


def _fid(f):
    ev = f.get("evidence") or {}
    return (f["rule"], ev.get("index"), ev.get("ledger_ts"), f.get("detail"))


class Watcher:
    def __init__(self, charter, ledger=None, notify_cmd=None, emit=None,
                 craft=False, scene="SOCIAL", pcs=()):
        self.notify_cmd = notify_cmd
        sink = emit or (lambda e: print(json.dumps(e), flush=True))
        self.emit = lambda event: sink(redact_output(event, self.ch or {}))
        self.messages = []
        self.open = {}
        self._fatal = None
        try:
            self.ch = normalize_charter(charter, require_gm=True)
            self.ledger = normalize_ledger(ledger)
        except InputValidationError as exc:
            self.ch = None
            self.ledger = []
            self._fatal = invalid_result(exc.issues, mode="live")
        # v0.5.1 attention lane in the loop: advisory events, NEVER findings.
        self.craft = craft
        self.scene = scene.upper() if isinstance(scene, str) else "SOCIAL"
        self.pcs = tuple(pcs) if isinstance(pcs, (list, tuple)) else ()
        if self._fatal is None and not isinstance(self.craft, bool):
            self._fatal = invalid_result([
                issue("craft.boolean", "/craft", "craft must be a boolean")
            ], mode="live", charter=self.ch)
        if self._fatal is None and self.craft:
            craft_problems = []
            if self.scene not in ("COMBAT", "SOCIAL", "EXPLORATION"):
                craft_problems.append(issue(
                    "craft.scene", "/scene",
                    "scene must be COMBAT, SOCIAL, or EXPLORATION"))
            seen_pcs = set()
            for index, pc in enumerate(self.pcs):
                if not isinstance(pc, str) or not pc.strip():
                    craft_problems.append(issue(
                        "craft.name_type", "/pc_names/%d" % index,
                        "PC names must be nonempty strings"))
                elif pc.casefold() in seen_pcs:
                    craft_problems.append(issue(
                        "craft.name_duplicate", "/pc_names/%d" % index,
                        "PC names must not be duplicated"))
                else:
                    seen_pcs.add(pc.casefold())
            if craft_problems:
                self._fatal = invalid_result(craft_problems, mode="live",
                                             charter=self.ch)
        self._last_notice = None
        self._quiet_flagged = set()

    def fail(self, problems):
        if self._fatal is None:
            self._fatal = invalid_result(problems, mode="live",
                                         messages=len(self.messages),
                                         charter=self.ch)
            # Once the evidence stream is unusable, no previously opened
            # finding remains an authoritative current assertion.
            self.open.clear()
            self.emit({"event": "evaluation", **self._fatal.to_dict()})
        return self._fatal

    def _evaluate(self, closed=False, now=None):
        if self._fatal is not None:
            return self._fatal
        result = evaluate(self.messages, self.ch, self.ledger,
                          mode="closed" if closed else "live", now=now)
        if result.status == "invalid":
            return self.fail(result.errors)
        cur = {_fid(f): f for f in result.findings}
        for fid, f in cur.items():
            if fid not in self.open:
                self.open[fid] = f
                self.emit({"event": "open", **f})
                if self.notify_cmd:
                    try:
                        subprocess.run(self.notify_cmd, shell=True,
                                       input=json.dumps(f), text=True,
                                       timeout=30, check=False)
                    except Exception:  # noqa: BLE001 — notify must never kill the watch
                        pass
        for fid in [k for k in self.open if k not in cur]:
            self.emit({"event": "resolved", **self.open.pop(fid)})
        return result

    def feed(self, msg, now=None):
        if self._fatal is not None:
            return self._fatal
        try:
            msg = normalize_transcript([msg])[0]
        except InputValidationError as exc:
            index = len(self.messages)
            rebased = [issue(
                problem.code,
                problem.pointer.replace("/transcript/0",
                                        "/transcript/%d" % index, 1),
                problem.message) for problem in exc.issues]
            return self.fail(rebased)
        msg["i"] = len(self.messages)
        self.messages.append(msg)
        self._evaluate(closed=False, now=now if now is not None else time.time())
        if self.craft and msg.get("author") in self.ch.get("gm", []):
            from .craft import attention, hard_defects
            beats = [m["content"] for m in self.messages
                     if m.get("author") in self.ch["gm"]]
            hd = hard_defects(msg.get("content", ""), self.pcs)
            if hd:
                self.emit({"event": "craft_defect", "beat": len(beats) - 1,
                           "defects": hd, "advisory": True})
            # seat starvation (rule 69): a player seat silent across N GM
            # beats while the scene advances. Advisory - people step away.
            n_gm = len(beats)
            quiet_after = int(self.ch.get("seat_quiet_gm_beats", 3))
            for pc in self.pcs:
                seat_msgs = [m["i"] for m in self.messages
                             if pc.lower() in (m.get("author") or "").lower()]
                last = seat_msgs[-1] if seat_msgs else -1
                gm_since = sum(1 for m in self.messages[last + 1:]
                               if m.get("author") in self.ch["gm"])
                if gm_since >= quiet_after and (pc, n_gm) not in self._quiet_flagged                         and pc not in {x for x, _ in self._quiet_flagged if _ >= n_gm - 1}:
                    self._quiet_flagged.add((pc, n_gm))
                    self.emit({"event": "seat_quiet", "seat": pc,
                               "gm_beats_since_last_action": gm_since,
                               "advisory": True,
                               "suggest": "offer the same-timeframe: price the elapsed action in turns, then ask what this character was doing during it (retroactive backfill ok - rule 69b)"})
            a = attention(beats, self.scene)
            notice = a and a["notice"]
            if notice != self._last_notice:   # resolve-and-move-on, no nagging
                self._last_notice = notice
                if a:
                    self.emit({"event": "craft_attention", **a, "advisory": True})

    def tick(self, now=None):
        return self._evaluate(closed=False,
                              now=now if now is not None else time.time())

    def close(self):
        if self._fatal is not None:
            result = self._fatal
        else:
            result = self._evaluate(closed=True)
        summary = {"event": "session_end", **result.to_dict(),
                   "open": sorted({f["rule"] for f in self.open.values()}),
                   "open_count": len(self.open)}
        self.emit(summary)
        return result.exit_code


def watch_main(a):
    """CLI: dmcheck watch <file|-> [--follow] — stdin or tail a growing file."""
    try:
        ch = load_charter(a.charter, gm=a.gm, dice_authors=a.dice_bot)
        ledger = load_ledger(a.ledger)
    except InputValidationError as exc:
        result = invalid_result(exc.issues, mode="live")
        print(json.dumps({"event": "session_end", **result.to_dict(),
                          "open": [], "open_count": 0}))
        return 2
    w = Watcher(ch, ledger, notify_cmd=a.notify_cmd,
                craft=getattr(a, "craft", False), scene=getattr(a, "scene", "SOCIAL"),
                pcs=tuple(getattr(a, "pc", None) or []))
    try:
        if a.transcript == "-":
            stream = getattr(sys.stdin, "buffer", sys.stdin)
            for line_number, line in enumerate(stream, 1):
                if isinstance(line, bytes):
                    try:
                        line = line.decode("utf-8")
                    except UnicodeDecodeError:
                        w.fail([issue("input.utf8",
                                      "/transcript/line/%d" % line_number,
                                      "transcript stdin must be valid UTF-8")])
                        break
                line = line.strip()
                if line:
                    try:
                        message = parse_json_value(
                            line, "/transcript/line/%d" % line_number)
                    except InputValidationError as exc:
                        w.fail(exc.issues)
                        break
                    timestamp = message.get("ts", message.get("timestamp")) \
                        if isinstance(message, dict) else None
                    w.feed(message, now=timestamp)
        else:
            for m in load_transcript(a.transcript):
                w.feed(m, now=m.get("ts"))
            if a.follow:
                import os
                pos = os.path.getsize(a.transcript)
                while True:
                    time.sleep(a.interval)
                    size = os.path.getsize(a.transcript)
                    if size > pos:
                        with open(a.transcript, encoding="utf-8") as f:
                            f.seek(pos)
                            for line in f:
                                if line.strip():
                                    try:
                                        message = parse_json_value(
                                            line, "/transcript")
                                        timestamp = message.get(
                                            "ts", message.get("timestamp")) \
                                            if isinstance(message, dict) else None
                                        w.feed(message, now=timestamp)
                                    except InputValidationError as exc:
                                        w.fail(exc.issues)
                                        break
                        pos = size
                        if w._fatal is not None:
                            break
                    else:
                        w.tick()
    except InputValidationError as exc:
        w.fail(exc.issues)
    except (OSError, UnicodeError) as exc:
        w.fail([issue("input.unreadable", "/transcript",
                      "transcript could not be read: %s" % exc)])
    except (KeyboardInterrupt, EOFError):
        pass
    return w.close()
