"""
core/scheduler.py — persistent cron / interval / one-shot scheduler (P2.2).

Stdlib only. Runs beside the agent worker as an asyncio task. On each tick
(15s heartbeat by default) it scans the `schedules` table for rows whose
`next_run_at` has passed, creates a `tasks` entry per goal via
`core.tasks.create_task`, updates `last_run_at` and recomputes
`next_run_at`.

Why we do not use APScheduler here:
- One fewer dependency for a feature with a well-defined scope (minute-grain
  cron + interval + one-shot). The tasks SQLite already proves the
  persistence pattern.
- The idempotency key from P1.2 is enough to prevent duplicate dispatch
  even if the scheduler ticks before the previous tick's write committed —
  we pass a stable `schedule:<id>:<iso-slot>` key per fire.

Cron grammar (minimal, enough for everyday schedules):
  5 fields separated by whitespace: minute hour day-of-month month day-of-week
  Each field: `*`, `N`, `A-B`, `A-B/S`, `*/S`, `A,B,C` (combinations allowed).
  Day-of-week: 0 (Sunday) .. 6 (Saturday). `MON-FRI` style aliases are NOT
  supported — numbers only.

The hand-rolled parser is deliberately small; it validates each field on
add and raises `ValueError` before persisting a malformed expression.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from core import tasks as _tasks
from core.logging_setup import get_logger

_log = get_logger(__name__)

_TICK_SECS = 15.0

_KINDS = {"cron", "interval", "date"}


# ── Cron parsing ─────────────────────────────────────────────────────────────

_FIELD_BOUNDS = (
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("dom", 1, 31),
    ("month", 1, 12),
    ("dow", 0, 6),
)


def _parse_field(expr: str, lo: int, hi: int) -> set[int]:
    out: set[int] = set()
    for piece in expr.split(","):
        step = 1
        if "/" in piece:
            piece, step_s = piece.split("/", 1)
            step = int(step_s)
            if step <= 0:
                raise ValueError(f"step must be positive: {expr}")
        if piece == "*":
            start, end = lo, hi
        elif "-" in piece:
            a, b = piece.split("-", 1)
            start, end = int(a), int(b)
        else:
            start = end = int(piece)
        if start < lo or end > hi or start > end:
            raise ValueError(f"{expr!r} out of {lo}..{hi}")
        out.update(range(start, end + 1, step))
    return out


def parse_cron(expr: str) -> tuple[set[int], set[int], set[int], set[int], set[int]]:
    fields = expr.strip().split()
    if len(fields) != 5:
        raise ValueError(f"cron needs 5 fields: {expr!r}")
    return tuple(_parse_field(f, lo, hi) for f, (_, lo, hi) in zip(fields, _FIELD_BOUNDS))  # type: ignore[return-value]


def next_cron_after(expr: str, after: _dt.datetime) -> _dt.datetime:
    """Return the next minute-aligned datetime at which `expr` fires strictly
    after `after`. Minute precision is enough; this isn't a sub-second
    orchestrator."""
    mins, hours, doms, months, dows = parse_cron(expr)
    # Start at the next whole minute strictly after `after`.
    cur = (after.replace(second=0, microsecond=0) + _dt.timedelta(minutes=1))
    # Cap the search — a well-formed cron fires within a year, so bail if
    # we somehow walk past that. Protects against impossible expressions
    # (Feb 30 etc).
    limit = after + _dt.timedelta(days=366)
    while cur <= limit:
        if (cur.minute in mins
                and cur.hour in hours
                and cur.day in doms
                and cur.month in months
                and _weekday_to_cron(cur) in dows):
            return cur
        cur += _dt.timedelta(minutes=1)
    raise ValueError(f"no match for cron {expr!r} within a year of {after}")


def _weekday_to_cron(dt: _dt.datetime) -> int:
    """Python weekday(): Monday=0..Sunday=6. Cron DOW: Sunday=0..Saturday=6."""
    return (dt.weekday() + 1) % 7


# ── Persistence ──────────────────────────────────────────────────────────────

class Scheduler:
    """Persistent scheduler. Owns one SQLite connection and one asyncio tick
    task. Thread-safe — the DB lock lets `add/remove/list` be called from any
    thread while the tick runs in the asyncio loop."""

    def __init__(self, db_path: Path | str,
                 create_task_fn: Callable[[str, Optional[str]], int] | None = None,
                 tick_secs: float = _TICK_SECS,
                 clock: Callable[[], float] | None = None):
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._tick_secs = tick_secs
        self._clock = clock or time.time
        self._create_task = create_task_fn or _default_create_task
        self._conn = sqlite3.connect(
            str(self._path), check_same_thread=False,
            detect_types=sqlite3.PARSE_DECLTYPES,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()
        self._stop = asyncio.Event()

    def _init_schema(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS schedules (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                name          TEXT    NOT NULL DEFAULT '',
                kind          TEXT    NOT NULL,
                cron_expr     TEXT    DEFAULT NULL,
                interval_s    REAL    DEFAULT NULL,
                run_at        REAL    DEFAULT NULL,
                goal          TEXT    NOT NULL,
                meta_json     TEXT    NOT NULL DEFAULT '{}',
                enabled       INTEGER NOT NULL DEFAULT 1,
                last_run_at   REAL    DEFAULT NULL,
                next_run_at   REAL    NOT NULL,
                created_at    REAL    NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_schedule_due
                ON schedules(enabled, next_run_at);
        """)
        self._conn.commit()

    # ── Public API ───────────────────────────────────────────────────────

    def add_cron(self, name: str, cron_expr: str, goal: str,
                 meta: dict | None = None, now: float | None = None) -> int:
        now = now if now is not None else self._clock()
        next_run = next_cron_after(cron_expr,
                                   _dt.datetime.fromtimestamp(now)).timestamp()
        return self._insert("cron", name, goal, meta, now, next_run,
                            cron_expr=cron_expr)

    def add_interval(self, name: str, interval_s: float, goal: str,
                     meta: dict | None = None,
                     start_at: float | None = None,
                     now: float | None = None) -> int:
        if interval_s <= 0:
            raise ValueError("interval_s must be positive")
        now = now if now is not None else self._clock()
        next_run = start_at if start_at is not None else now + interval_s
        return self._insert("interval", name, goal, meta, now, next_run,
                            interval_s=interval_s)

    def add_once(self, name: str, run_at: float, goal: str,
                 meta: dict | None = None, now: float | None = None) -> int:
        now = now if now is not None else self._clock()
        return self._insert("date", name, goal, meta, now, run_at,
                            run_at=run_at)

    def remove(self, schedule_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM schedules WHERE id=?", (schedule_id,))
            self._conn.commit()

    def set_enabled(self, schedule_id: int, enabled: bool) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE schedules SET enabled=? WHERE id=?",
                (1 if enabled else 0, schedule_id),
            )
            self._conn.commit()

    def list(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM schedules ORDER BY next_run_at"
            ).fetchall()
            return [dict(r) for r in rows]

    def get(self, schedule_id: int) -> dict | None:
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM schedules WHERE id=?", (schedule_id,),
            ).fetchone()
            return dict(r) if r else None

    # ── Tick ─────────────────────────────────────────────────────────────

    def tick(self, now: float | None = None) -> int:
        """Fire every schedule whose next_run_at has passed. Returns the
        number of task creations. Called on an asyncio tick by `run()`;
        callable directly in tests with a synthetic clock."""
        now = now if now is not None else self._clock()
        fired = 0
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM schedules WHERE enabled=1 AND next_run_at<=? "
                "ORDER BY next_run_at",
                (now,),
            ).fetchall()
        for row in rows:
            fired += 1 if self._fire(dict(row), now) else 0
        return fired

    def _fire(self, row: dict, now: float) -> bool:
        goal = row["goal"]
        # Stable key — same slot cannot be fired twice, even if a crash
        # mid-tick leaves `last_run_at` unchanged.
        slot_iso = _dt.datetime.fromtimestamp(row["next_run_at"]).isoformat(timespec="minutes")
        key = f"schedule:{row['id']}:{slot_iso}"
        try:
            self._create_task(goal, key)
        except Exception as e:
            _log.error("schedule_fire_failed", extra={
                "schedule_id": row["id"], "err": str(e),
            })
            return False
        # Walk the next_run_at forward.
        next_run = self._advance(row, now)
        with self._lock:
            if next_run is None:
                # One-shot — disable rather than delete, so `list()` can still
                # show history until the user explicitly removes it.
                self._conn.execute(
                    "UPDATE schedules SET enabled=0, last_run_at=? WHERE id=?",
                    (now, row["id"]),
                )
            else:
                self._conn.execute(
                    "UPDATE schedules SET last_run_at=?, next_run_at=? WHERE id=?",
                    (now, next_run, row["id"]),
                )
            self._conn.commit()
        _log.info("schedule_fired", extra={
            "schedule_id": row["id"], "sched_name": row["name"], "goal": goal[:80],
        })
        return True

    def _advance(self, row: dict, now: float) -> float | None:
        kind = row["kind"]
        if kind == "date":
            return None
        if kind == "interval":
            step = float(row["interval_s"])
            nxt = float(row["next_run_at"]) + step
            # Catch up if the scheduler slept through several windows.
            while nxt <= now:
                nxt += step
            return nxt
        if kind == "cron":
            nxt = next_cron_after(row["cron_expr"],
                                  _dt.datetime.fromtimestamp(now)).timestamp()
            return nxt
        raise ValueError(f"unknown schedule kind: {kind}")

    async def run(self) -> None:
        """Background tick loop. Stop via `stop()`."""
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:
                _log.error("scheduler_tick_err", extra={"err": str(e)})
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._tick_secs)
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()

    # ── Internal ─────────────────────────────────────────────────────────

    def _insert(self, kind: str, name: str, goal: str, meta: dict | None,
                now: float, next_run_at: float,
                cron_expr: str | None = None,
                interval_s: float | None = None,
                run_at: float | None = None) -> int:
        if kind not in _KINDS:
            raise ValueError(f"unknown kind: {kind}")
        if not goal.strip():
            raise ValueError("goal cannot be blank")
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO schedules(name, kind, cron_expr, interval_s, "
                "run_at, goal, meta_json, enabled, next_run_at, created_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                (name, kind, cron_expr, interval_s, run_at, goal,
                 json.dumps(meta or {}), next_run_at, now),
            )
            self._conn.commit()
            return int(cur.lastrowid)


def _default_create_task(goal: str, idempotency_key: str | None) -> int:
    return _tasks.create_task(goal, idempotency_key=idempotency_key)
