"""dmcheck live mode — stable finding lifecycle over growing JSONL sources.

The batch checker remains the verdict engine. Watch adds deterministic finding
identity, at most one OPEN and RESOLVED per source obligation, conservative terminal
incomplete state, and a robust repo-local follower for transcript and ledger
JSONL. Canonical durable lifecycle state remains a PORT-001/PORT-002 concern.
"""
import json
import os
import subprocess
import sys
import time

from .core import (_source_row_key, evaluate, incomplete_obligations,
                   invalid_result, load_charter, load_ledger, load_transcript,
                   redact_output)
from .validation import (InputValidationError, issue, normalize_charter,
                         normalize_ledger, normalize_transcript)


def _fid(finding):
    return finding["finding_id"]


def _is_actionable(finding):
    return (finding.get("status", "open") == "open"
            and finding.get("severity", "finding") == "finding"
            and finding.get("provenance", "observed") == "observed")


class JsonlFollower:
    """Commit newline-terminated JSONL rows with an exportable local cursor.

    The cursor is intentionally a repo-local handoff, not a portfolio event
    contract. It supports safe cursor handoff while the embedding retains its
    Watcher; persisting full evaluation state across process failure is
    deferred to PORT-001/PORT-002.
    """

    CURSOR_VERSION = 1

    def __init__(self, path, source, emit=None, cursor=None):
        if source not in ("transcript", "ledger"):
            raise ValueError("source must be 'transcript' or 'ledger'")
        self.path = os.path.abspath(path)
        self.source = source
        self.emit = emit or (lambda event: None)
        self.offset = 0
        self.identity = None
        self.generation = 0
        self.observed_size = 0
        self.anchor = b""
        self.seen = set()
        self.last_ts = None
        self.coverage_complete = True
        self.partial = False
        self.missing = False
        self._health_keys = set()

        if cursor and cursor.get("version") == self.CURSOR_VERSION \
                and cursor.get("path") == self.path \
                and cursor.get("source") == self.source:
            self.offset = max(0, int(cursor.get("offset", 0)))
            identity = cursor.get("identity")
            self.identity = tuple(identity) if identity else None
            self.generation = max(0, int(cursor.get("generation", 0)))
            self.observed_size = max(
                self.offset, int(cursor.get("observed_size", self.offset)))
            try:
                self.anchor = bytes.fromhex(cursor.get("anchor", ""))
            except (TypeError, ValueError):
                self.anchor = b""
            self.seen = set(cursor.get("seen") or [])
            self.last_ts = cursor.get("last_ts")
            self.coverage_complete = bool(
                cursor.get("coverage_complete", True))
            self.partial = bool(cursor.get("partial", False))

    @property
    def coverage(self):
        if not self.coverage_complete:
            return "incomplete"
        if self.partial or self.missing:
            return "pending"
        return "complete"

    def cursor(self):
        return {
            "version": self.CURSOR_VERSION,
            "path": self.path,
            "source": self.source,
            "offset": self.offset,
            "identity": list(self.identity) if self.identity else None,
            "generation": self.generation,
            "observed_size": self.observed_size,
            "anchor": self.anchor.hex(),
            "seen": sorted(self.seen),
            "last_ts": self.last_ts,
            "coverage_complete": self.coverage_complete,
            "partial": self.partial,
        }

    def _health(self, reason, status, key=None, **detail):
        signature = (self.generation, reason, key)
        if signature in self._health_keys:
            return
        self._health_keys.add(signature)
        self.emit({
            "event": "source_health",
            "source": self.source,
            "path": self.path,
            "status": status,
            "coverage": self.coverage,
            "reason": reason,
            **detail,
        })

    def _reset(self, identity, reason, **detail):
        self.generation += 1
        self.coverage_complete = False
        self.offset = 0
        self.identity = identity
        self.observed_size = 0
        self.anchor = b""
        self.partial = False
        self._health(reason, "degraded", key=self.generation, **detail)

    def _normalize(self, row):
        if self.source == "transcript":
            return normalize_transcript([row])[0]
        return normalize_ledger([row])[0]

    def poll(self):
        try:
            stat = os.stat(self.path)
        except FileNotFoundError:
            self.missing = True
            self._health("source_unavailable", "waiting", key=self.offset)
            return []
        except OSError as exc:
            self.missing = True
            self._health("source_unreadable", "waiting", key=self.offset,
                         detail=str(exc))
            return []

        identity = (stat.st_dev, stat.st_ino)
        if self.missing:
            self.missing = False
            self._health("source_available", "recovered", key=identity)
        if self.identity is None:
            self.identity = identity
        elif identity != self.identity:
            self._reset(identity, "rotation_detected")
        elif stat.st_size < self.observed_size:
            self._reset(identity, "truncation_detected",
                        previous_offset=self.offset,
                        previous_size=self.observed_size,
                        size=stat.st_size)

        try:
            with open(self.path, "rb") as handle:
                if self.offset and self.anchor:
                    anchor_start = self.offset - len(self.anchor)
                    handle.seek(anchor_start)
                    if handle.read(len(self.anchor)) != self.anchor:
                        self._reset(identity, "rewrite_detected",
                                    previous_offset=self.offset,
                                    size=stat.st_size)
                handle.seek(self.offset)
                data = handle.read()
        except OSError as exc:
            self.missing = True
            self._health("source_unreadable", "waiting", key=self.offset,
                         detail=str(exc))
            return []

        self.observed_size = max(stat.st_size, self.offset + len(data))
        if not data:
            return []
        newline = data.rfind(b"\n")
        if newline < 0:
            self.partial = True
            self._health("partial_line", "waiting", key=self.offset,
                         pending_bytes=len(data))
            return []

        committed = data[:newline + 1]
        remainder = data[newline + 1:]
        prior_partial = self.partial
        prior_anchor = self.anchor
        self.offset += len(committed)
        self.anchor = (prior_anchor + committed)[-64:]
        self.partial = bool(remainder)

        rows = []
        duplicates = 0
        invalid = 0
        out_of_order = 0
        for raw_line in committed.splitlines():
            if not raw_line.strip():
                continue
            try:
                decoded = raw_line.decode("utf-8")
                value = json.loads(decoded)
                if not isinstance(value, dict):
                    raise ValueError("row must be a JSON object")
                row = self._normalize(value)
                if self.source == "transcript" and row.get("author") is None:
                    raise ValueError("transcript row has no author")
            except (UnicodeDecodeError, json.JSONDecodeError,
                    TypeError, ValueError):
                invalid += 1
                continue

            key = _source_row_key(self.source, row)
            if key in self.seen:
                duplicates += 1
                continue
            self.seen.add(key)

            timestamp = row.get("ts")
            if timestamp is not None:
                if self.last_ts is not None and timestamp < self.last_ts:
                    out_of_order += 1
                self.last_ts = (timestamp if self.last_ts is None
                                else max(self.last_ts, timestamp))
            rows.append(row)

        if invalid:
            self.coverage_complete = False
            self._health("invalid_complete_row", "degraded",
                         key=self.offset, rows=invalid)
        if out_of_order:
            self.coverage_complete = False
            self._health("out_of_order_row", "degraded",
                         key=self.offset, rows=out_of_order)
        if duplicates:
            self._health("duplicate_delivery_ignored", "ok",
                         key=self.offset, rows=duplicates)
        if self.partial:
            self._health("partial_line", "waiting", key=self.offset,
                         pending_bytes=len(remainder))
        elif prior_partial:
            self._health("partial_line_committed", "recovered",
                         key=self.offset)
        return rows


class Watcher:
    def __init__(self, charter, ledger=None, notify_cmd=None, emit=None,
                 craft=False, scene="SOCIAL", pcs=()):
        self.notify_cmd = notify_cmd
        sink = emit or (lambda event: print(json.dumps(event), flush=True))
        self._fatal = None
        try:
            self.ch = normalize_charter(charter, require_gm=True)
            initial_ledger = normalize_ledger(ledger)
        except InputValidationError as exc:
            self.ch = None
            initial_ledger = []
            self._fatal = invalid_result(exc.issues, mode="live")
        self.emit = lambda event: sink(redact_output(event, self.ch or {}))
        self.messages = []
        self.ledger = []
        self.open = {}
        self._message_keys = set()
        self._ledger_keys = set()
        self._opened_ids = set()
        self._resolved_ids = set()
        self._last_now = None
        self._closed = False
        self._close_code = 0
        self.paused = False
        self.source_coverage = {}
        self.source_health = {}

        # v0.5.1 attention lane in the loop: advisory events, NEVER findings.
        self.craft = craft
        self.scene = scene.upper() if isinstance(scene, str) else "SOCIAL"
        self.pcs = tuple(pcs) if isinstance(pcs, (list, tuple)) else ()
        self._last_notice = None
        self._quiet_flagged = set()
        if self._fatal is None and not isinstance(self.craft, bool):
            self._fatal = invalid_result([
                issue("craft.boolean", "/craft", "craft must be a boolean")
            ], mode="live", charter=self.ch)
        for event in initial_ledger:
            self._append_ledger(event)

    def record_health(self, event):
        source = event.get("source", "unknown")
        self.source_coverage[source] = event.get("coverage", "incomplete")
        self.source_health[source] = dict(event)
        self.emit(dict(event))

    def set_source_coverage(self, source, coverage):
        self.source_coverage[source] = coverage

    def _evaluate(self, closed=False, now=None):
        if self._fatal is not None:
            return self._fatal
        if now is not None:
            self._last_now = now
        if self.paused and not closed:
            return
        result = evaluate(self.messages, self.ch, self.ledger,
                          mode="closed" if closed else "live", now=now)
        if result.status == "invalid":
            self._fatal = result
            self.open.clear()
            self.emit({"event": "evaluation", **result.to_dict()})
            return result
        current = {_fid(finding): finding for finding in result.findings}
        previous = self.open
        next_open = {}
        for finding_id, finding in current.items():
            # Always retain the checker's canonical terminal order/evidence.
            # Mutable detail (for example R7 elapsed seconds) refreshes the
            # snapshot without creating a new lifecycle episode.
            next_open[finding_id] = finding
            if finding_id not in previous and finding_id not in self._opened_ids:
                self._opened_ids.add(finding_id)
                self.emit({"event": "open", **finding})
                if self.notify_cmd and _is_actionable(finding):
                    try:
                        payload = redact_output(finding, self.ch)
                        subprocess.run(self.notify_cmd, shell=True,
                                       input=json.dumps(payload), text=True,
                                       timeout=30, check=False)
                    except Exception:  # notify must never kill the watch
                        pass
        for finding_id in [key for key in previous if key not in current]:
            finding = previous[finding_id]
            if finding_id not in self._resolved_ids:
                self._resolved_ids.add(finding_id)
                self.emit({"event": "resolved", **finding})
        self.open = next_open
        return result

    def _append_message(self, message):
        try:
            row = normalize_transcript([message])[0]
        except InputValidationError as exc:
            self._fatal = invalid_result(exc.issues, mode="live",
                                         messages=len(self.messages),
                                         charter=self.ch)
            self.emit({"event": "evaluation", **self._fatal.to_dict()})
            return None
        if row.get("author") is None:
            return None
        key = _source_row_key("transcript", row)
        if key in self._message_keys:
            return None
        self._message_keys.add(key)
        row["i"] = len(self.messages)
        self.messages.append(row)
        return row

    def _append_ledger(self, event):
        try:
            row = normalize_ledger([event])[0]
        except InputValidationError as exc:
            self._fatal = invalid_result(exc.issues, mode="live",
                                         messages=len(self.messages),
                                         charter=self.ch)
            self.emit({"event": "evaluation", **self._fatal.to_dict()})
            return None
        key = _source_row_key("ledger", row)
        if key in self._ledger_keys:
            return None
        self._ledger_keys.add(key)
        self.ledger.append(row)
        return row

    def ingest(self, messages=(), ledger=(), now=None):
        if self._closed:
            return False
        changed = False
        new_messages = []
        for event in ledger:
            changed = self._append_ledger(event) is not None or changed
        for message in messages:
            row = self._append_message(message)
            if row is not None:
                changed = True
                new_messages.append(row)
        if changed or now is not None:
            self._evaluate(closed=False, now=now)
        if not self.paused:
            for message in new_messages:
                self._craft_message(message)
        return changed

    def feed(self, message, now=None):
        effective_now = now if now is not None else time.time()
        return self.ingest(messages=(message,), now=effective_now)

    def feed_ledger(self, event, now=None):
        effective_now = now if now is not None else time.time()
        return self.ingest(ledger=(event,), now=effective_now)

    def _craft_message(self, message):
        if not self.craft or message.get("author") not in self.ch.get("gm", []):
            return
        from .craft import attention, hard_defects
        beats = [row["content"] for row in self.messages
                 if row.get("author") in self.ch["gm"]]
        defects = hard_defects(message.get("content", ""), self.pcs)
        if defects:
            self.emit({"event": "craft_defect", "beat": len(beats) - 1,
                       "defects": defects, "advisory": True})

        n_gm = len(beats)
        quiet_after = int(self.ch.get("seat_quiet_gm_beats", 3))
        for pc in self.pcs:
            seat_messages = [row["i"] for row in self.messages
                             if pc.lower() in (row.get("author") or "").lower()]
            last = seat_messages[-1] if seat_messages else -1
            gm_since = sum(1 for row in self.messages[last + 1:]
                           if row.get("author") in self.ch["gm"])
            recently_flagged = {
                seat for seat, beat in self._quiet_flagged if beat >= n_gm - 1
            }
            if gm_since >= quiet_after and (pc, n_gm) not in self._quiet_flagged \
                    and pc not in recently_flagged:
                self._quiet_flagged.add((pc, n_gm))
                self.emit({
                    "event": "seat_quiet", "seat": pc,
                    "gm_beats_since_last_action": gm_since,
                    "advisory": True,
                    "suggest": "offer the same-timeframe: price the elapsed "
                               "action in turns, then ask what this character "
                               "was doing during it (retroactive backfill ok "
                               "- rule 69b)",
                })
        signal = attention(beats, self.scene)
        notice = signal and signal["notice"]
        if notice != self._last_notice:
            self._last_notice = notice
            if signal:
                self.emit({"event": "craft_attention", **signal,
                           "advisory": True})

    def tick(self, now=None):
        if self._closed or self.paused:
            return
        self._evaluate(closed=False,
                       now=now if now is not None else time.time())

    def pause(self):
        if not self._closed and not self.paused:
            self.paused = True
            self.emit({"event": "session_paused", "state": "paused"})

    def resume(self, now=None):
        if not self._closed and self.paused:
            self.paused = False
            self.emit({"event": "session_resumed", "state": "running"})
            self._evaluate(closed=False,
                           now=now if now is not None else time.time())

    def close(self, now=None):
        if self._closed:
            return self._close_code
        terminal_now = now if now is not None else self._last_now
        result = self._evaluate(closed=True, now=terminal_now)
        if result is None:
            result = self._fatal or invalid_result([
                issue("evaluation.failed", "/evaluation",
                      "live evaluation did not produce a result")
            ], mode="closed", charter=self.ch)
        incomplete = ([] if result.status == "invalid" else
                      incomplete_obligations(
                          self.messages, self.ch, self.ledger,
                          now=terminal_now))
        for source, coverage in sorted(self.source_coverage.items()):
            if coverage != "complete":
                health = self.source_health.get(source) or {}
                incomplete.append({
                    "scope": "source", "source": source,
                    "status": "incomplete", "coverage": coverage,
                    "reason": health.get("reason", "source_coverage_incomplete"),
                })
        # `_evaluate(closed=True)` rebuilds this mapping in the batch checker's
        # deterministic order, so this list is byte-for-byte comparable after
        # JSON serialization when both paths use the same evaluation horizon.
        findings = list(self.open.values())
        summary = {
            "event": "session_end", **result.to_dict(),
            "state": "ended",
            "evaluation_ts": terminal_now,
            # `open` remains the legacy rule-name summary.
            "open": sorted({finding["rule"] for finding in findings}),
            "open_count": len(findings),
            "finding_ids": [finding["finding_id"] for finding in findings],
            "findings": findings,
            "incomplete_count": len(incomplete),
            "incomplete": incomplete,
            "coverage": dict(sorted(self.source_coverage.items())),
        }
        self.emit(summary)
        self._close_code = result.exit_code
        self._closed = True
        return self._close_code


def poll_sources(watcher, transcript_follower=None, ledger_follower=None,
                 now=None):
    """Poll both sources, then evaluate their committed rows as one snapshot."""
    ledger_rows = ledger_follower.poll() if ledger_follower else []
    transcript_rows = (transcript_follower.poll()
                       if transcript_follower else [])
    if ledger_follower:
        watcher.set_source_coverage("ledger", ledger_follower.coverage)
    if transcript_follower:
        watcher.set_source_coverage("transcript", transcript_follower.coverage)
    watcher.ingest(messages=transcript_rows, ledger=ledger_rows, now=now)
    return {"transcript": len(transcript_rows), "ledger": len(ledger_rows)}


def watch_main(args):
    """CLI: dmcheck watch <file|-> [--follow] — stdin or growing JSONL."""
    try:
        charter = load_charter(args.charter, gm=args.gm,
                               dice_authors=args.dice_bot)
    except InputValidationError as exc:
        result = invalid_result(exc.issues, mode="live")
        print(json.dumps({"event": "session_end", **result.to_dict(),
                          "open": [], "open_count": 0}))
        return 2

    if args.transcript != "-" and args.follow:
        watcher = Watcher(
            charter, notify_cmd=args.notify_cmd,
            craft=getattr(args, "craft", False),
            scene=getattr(args, "scene", "SOCIAL"),
            pcs=tuple(getattr(args, "pc", None) or []))
        transcript_follower = JsonlFollower(
            args.transcript, "transcript", emit=watcher.record_health)
        ledger_follower = (JsonlFollower(
            args.ledger, "ledger", emit=watcher.record_health)
            if args.ledger else None)
        poll_sources(watcher, transcript_follower, ledger_follower)
        try:
            while True:
                time.sleep(args.interval)
                poll_sources(watcher, transcript_follower, ledger_follower,
                             now=time.time())
        except (KeyboardInterrupt, EOFError):
            pass
        return watcher.close()

    try:
        if args.transcript == "-":
            # Follow the ledger from byte zero too: preloading it and then
            # constructing a follower both rejected partial writes and read
            # the startup rows twice.
            watcher = Watcher(
                charter, notify_cmd=args.notify_cmd,
                craft=getattr(args, "craft", False),
                scene=getattr(args, "scene", "SOCIAL"),
                pcs=tuple(getattr(args, "pc", None) or []))
            ledger_follower = (JsonlFollower(
                args.ledger, "ledger", emit=watcher.record_health)
                if args.ledger else None)
            if ledger_follower:
                poll_sources(watcher, ledger_follower=ledger_follower)
            for line in sys.stdin:
                if line.strip():
                    ledger_rows = (ledger_follower.poll()
                                   if ledger_follower else [])
                    if ledger_follower:
                        watcher.set_source_coverage(
                            "ledger", ledger_follower.coverage)
                    watcher.ingest(
                        messages=(json.loads(line),), ledger=ledger_rows,
                        now=time.time())
            if ledger_follower:
                poll_sources(watcher, ledger_follower=ledger_follower)
        else:
            watcher = Watcher(
                charter, load_ledger(args.ledger),
                notify_cmd=args.notify_cmd,
                craft=getattr(args, "craft", False),
                scene=getattr(args, "scene", "SOCIAL"),
                pcs=tuple(getattr(args, "pc", None) or []))
            for message in load_transcript(args.transcript):
                watcher.feed(message, now=message.get("ts"))
    except (KeyboardInterrupt, EOFError):
        pass
    return watcher.close()
