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

from .core import check, load_charter, load_ledger, load_transcript


def _fid(f):
    ev = f.get("evidence") or {}
    return (f["rule"], ev.get("index"), ev.get("ledger_ts"), f.get("detail"))


class Watcher:
    def __init__(self, charter, ledger=None, notify_cmd=None, emit=None,
                 craft=False, scene="SOCIAL", pcs=()):
        self.ch = charter
        self.ledger = ledger or []
        self.notify_cmd = notify_cmd
        self.emit = emit or (lambda e: print(json.dumps(e), flush=True))
        self.messages = []
        self.open = {}
        # v0.5.1 attention lane in the loop: advisory events, NEVER findings.
        self.craft = craft
        self.scene = scene
        self.pcs = tuple(pcs)
        self._last_notice = None

    def _evaluate(self, closed=False, now=None):
        findings, _ = check(self.messages, self.ch, self.ledger,
                            closed=closed, now=now)
        if findings and "error" in findings[0]:
            self.emit({"event": "error", **findings[0]})
            return
        cur = {_fid(f): f for f in findings}
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

    def feed(self, msg, now=None):
        msg = dict(msg)
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
            a = attention(beats, self.scene)
            notice = a and a["notice"]
            if notice != self._last_notice:   # resolve-and-move-on, no nagging
                self._last_notice = notice
                if a:
                    self.emit({"event": "craft_attention", **a, "advisory": True})

    def tick(self, now=None):
        self._evaluate(closed=False, now=now if now is not None else time.time())

    def close(self):
        self._evaluate(closed=True)
        summary = {"event": "session_end",
                   "open": sorted({f["rule"] for f in self.open.values()}),
                   "open_count": len(self.open)}
        self.emit(summary)
        return 1 if self.open else 0


def watch_main(a):
    """CLI: dmcheck watch <file|-> [--follow] — stdin or tail a growing file."""
    ch = load_charter(a.charter)
    if a.gm:
        ch["gm"] = a.gm
    if a.dice_bot:
        ch["dice_authors"] = a.dice_bot
    ledger = load_ledger(a.ledger)
    w = Watcher(ch, ledger, notify_cmd=a.notify_cmd,
                craft=getattr(a, "craft", False), scene=getattr(a, "scene", "SOCIAL"),
                pcs=tuple(getattr(a, "pc", None) or []))
    try:
        if a.transcript == "-":
            for line in sys.stdin:
                line = line.strip()
                if line:
                    w.feed(json.loads(line))
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
                        with open(a.transcript) as f:
                            f.seek(pos)
                            for line in f:
                                if line.strip():
                                    w.feed(json.loads(line))
                        pos = size
                    else:
                        w.tick()
    except (KeyboardInterrupt, EOFError):
        pass
    return w.close()
