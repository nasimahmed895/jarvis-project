"""
core/notify.py — reliable notification delivery (P1.1 + P1.6).

The agent speaks completion lines directly through the voice/UI path. That is
fast but lossy: a crash between "task done" and the voice synth drops the
notification entirely. This module is the durable backstop. Every important
event (task completion, task failure, scheduler ping, external alert) is
enqueued here first, persisted to SQLite, then fanned out to any registered
sink with retry + dedup.

Guarantees:
- At-least-once delivery to every sink that does not permanently refuse.
- At-most-once per dedup_key (new enqueues with a key that already exists
  short-circuit to the existing row).
- Survival across restarts — `drain()` on a reopened database picks up any
  pending rows and continues retrying with its stored backoff schedule.

Non-goals:
- Ordering (sinks are independent; order within a sink is best-effort).
- Cross-machine replication.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Protocol

from core import journal as _journal
from core.logging_setup import get_logger

_log = get_logger(__name__)

# Retry schedule: attempt #0 is the first try, subsequent indices step up.
# After the last entry the row is marked dead_letter.
_BACKOFF_SECS: tuple[float, ...] = (0.0, 5.0, 30.0, 120.0)
_MAX_ATTEMPTS = len(_BACKOFF_SECS)


# ── Sink protocol + concrete sinks ───────────────────────────────────────────

class Notifier(Protocol):
    """A sink delivers a notification somewhere. Must be fast and must raise
    on transient failure so the queue can retry."""

    name: str

    def send(self, payload: dict) -> None: ...


class JournalNotifier:
    """Writes the notification into the structured event journal. Never fails."""

    name = "journal"

    def send(self, payload: dict) -> None:
        _journal.write("notify", **payload)


class TerminalNotifier:
    """Prints to stderr. On macOS, additionally shows a native toast via
    osascript — quietly skipped when the command is missing."""

    name = "terminal"

    def send(self, payload: dict) -> None:
        msg = str(payload.get("message") or payload.get("summary") or payload.get("kind", "notify"))
        title = str(payload.get("title") or "JARVIS")
        print(f"[NOTIFY] {title}: {msg}", file=sys.stderr, flush=True)
        if sys.platform == "darwin":
            try:
                import subprocess
                script = (
                    f'display notification {json.dumps(msg)} '
                    f'with title {json.dumps(title)}'
                )
                subprocess.run(
                    ["osascript", "-e", script],
                    check=False, timeout=3,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            except Exception:
                pass   # toast is a nice-to-have; do not fail the sink


class WebhookNotifier:
    """POST JSON to a configured URL. Opt-in — only active when a URL is supplied."""

    name = "webhook"

    def __init__(self, url: str, timeout_s: float = 5.0,
                 poster: Callable[[str, dict, float], int] | None = None):
        self._url = url
        self._timeout = timeout_s
        self._poster = poster or _default_post

    def send(self, payload: dict) -> None:
        status = self._poster(self._url, payload, self._timeout)
        if not (200 <= status < 300):
            raise RuntimeError(f"webhook returned HTTP {status}")


def _default_post(url: str, payload: dict, timeout_s: float) -> int:
    import urllib.request
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        return int(resp.status)


# ── Queue ────────────────────────────────────────────────────────────────────

class NotifyQueue:
    """Persistent, retry-safe notification queue."""

    def __init__(self, db_path: Path | str):
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._sinks: list[Notifier] = []
        self._conn = sqlite3.connect(
            str(self._path), check_same_thread=False,
            detect_types=sqlite3.PARSE_DECLTYPES,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS notifications (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                dedup_key       TEXT    DEFAULT NULL,
                payload_json    TEXT    NOT NULL,
                status          TEXT    NOT NULL DEFAULT 'pending',
                attempts        INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL    NOT NULL,
                created_at      REAL    NOT NULL,
                delivered_at    REAL    DEFAULT NULL,
                last_error      TEXT    DEFAULT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_notify_status
                ON notifications(status, next_attempt_at);
        """)
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_notify_dedup "
            "ON notifications(dedup_key) WHERE dedup_key IS NOT NULL"
        )
        self._conn.commit()

    def register_sink(self, sink: Notifier) -> None:
        with self._lock:
            self._sinks.append(sink)

    def clear_sinks(self) -> None:
        with self._lock:
            self._sinks.clear()

    def enqueue(self, payload: dict, dedup_key: str | None = None,
                now: float | None = None) -> int:
        """Insert a notification. Returns the row id. A duplicate dedup_key
        returns the existing id without inserting.

        `now` overrides the enqueue timestamp — tests use it to pin the queue
        against a synthetic clock passed to drain()."""
        now = now if now is not None else time.time()
        with self._lock:
            if dedup_key:
                row = self._conn.execute(
                    "SELECT id FROM notifications WHERE dedup_key=?",
                    (dedup_key,),
                ).fetchone()
                if row:
                    return int(row["id"])
            cur = self._conn.execute(
                "INSERT INTO notifications(dedup_key, payload_json, status, "
                "attempts, next_attempt_at, created_at) "
                "VALUES(?, ?, 'pending', 0, ?, ?)",
                (dedup_key, json.dumps(payload, default=str), now, now),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def drain(self, now: float | None = None) -> dict[str, int]:
        """Deliver every pending row whose next_attempt_at has passed.
        Returns a count of transitions: {delivered, retried, dead_letter}.
        Safe to call repeatedly from a scheduler / tick loop."""
        now = now if now is not None else time.time()
        delivered = retried = dead_letter = 0
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM notifications WHERE status='pending' "
                "AND next_attempt_at<=? ORDER BY id",
                (now,),
            ).fetchall()
            sinks = list(self._sinks)
        for row in rows:
            if not sinks:
                # Nothing to deliver to — leave the row pending so a later
                # register_sink() + drain() can handle it.
                continue
            payload = json.loads(row["payload_json"])
            outcome = _try_sinks(sinks, payload)
            if outcome.ok:
                self._mark_delivered(row["id"], now)
                delivered += 1
                continue
            attempts = row["attempts"] + 1
            if attempts >= _MAX_ATTEMPTS:
                self._mark_dead_letter(row["id"], outcome.error, now)
                dead_letter += 1
            else:
                delay = _BACKOFF_SECS[attempts]
                self._conn.execute(
                    "UPDATE notifications SET attempts=?, next_attempt_at=?, "
                    "last_error=? WHERE id=?",
                    (attempts, now + delay, outcome.error, row["id"]),
                )
                self._conn.commit()
                retried += 1
                _log.warning("notification retry", extra={
                    "nid": row["id"], "attempt": attempts, "delay_s": delay,
                    "error": outcome.error,
                })
        return {"delivered": delivered, "retried": retried, "dead_letter": dead_letter}

    def pending_count(self) -> int:
        with self._lock:
            return int(self._conn.execute(
                "SELECT COUNT(*) FROM notifications WHERE status='pending'"
            ).fetchone()[0])

    def row(self, nid: int) -> dict | None:
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM notifications WHERE id=?", (nid,)
            ).fetchone()
            return dict(r) if r else None

    def _mark_delivered(self, nid: int, now: float) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE notifications SET status='delivered', delivered_at=?, "
                "last_error=NULL WHERE id=?",
                (now, nid),
            )
            self._conn.commit()

    def _mark_dead_letter(self, nid: int, reason: str, now: float) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE notifications SET status='dead_letter', attempts=?, "
                "last_error=?, next_attempt_at=? WHERE id=?",
                (_MAX_ATTEMPTS, reason, now, nid),
            )
            self._conn.commit()
        _log.error("notification dead-lettered", extra={"nid": nid, "reason": reason})


class _SinkOutcome:
    __slots__ = ("ok", "error")

    def __init__(self, ok: bool, error: str | None):
        self.ok = ok
        self.error = error


def _try_sinks(sinks: list[Notifier], payload: dict) -> _SinkOutcome:
    """A notification is delivered only when every sink succeeds. The first
    raising sink short-circuits and returns its error — so a flaky webhook
    does not drop a message that the journal already received.

    Order is caller-defined (register_sink order)."""
    for s in sinks:
        try:
            s.send(payload)
        except Exception as e:
            return _SinkOutcome(False, f"{s.name}: {type(e).__name__}: {str(e)[:160]}")
    return _SinkOutcome(True, None)
