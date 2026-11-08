"""
SQLite-backed task/step store for the agent loop.

A session can plan a goal into steps, dispatch them to tools, verify each
outcome, and resume across restarts. Deliberately small: two tables, one
connection, one lock. No ORM.

Schema:
    tasks(id, goal, status, created_at, updated_at, result, parent_id)
    steps(id, task_id, idx, verb, tool, args_json, deps_json, verify_json,
          status, result, error, started_at, finished_at)

Status values (task + step):
    planned        — waiting for the planner to finish or for deps
    ready          — step: deps are all done
    running        — being dispatched right now
    done           — success, result recorded
    failed         — tool raised or verifier said no
    awaiting_user  — paused on a confirmation gate
    cancelled      — user killed it
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

_DB_LOCK = threading.RLock()
_DB: sqlite3.Connection | None = None
_DB_PATH: Path | None = None

# ── Change subscribers (P1.5 event-driven worker wake) ──────────────────────
#
# The worker previously polled every 0.5s. That is a noticeable wake-up gap
# between "user said do X" and "step starts running". Any function that
# mutates task/step state fires `_notify_subscribers()` so a subscriber can
# flip an asyncio.Event in its own loop (via `loop.call_soon_threadsafe`)
# and pick the work up immediately. Subscribers are raw callables kept
# short — they must not do real work; just signal.
_SUBSCRIBERS: list = []


def subscribe(cb) -> None:
    """Register a zero-arg callable fired after every state mutation.
    Safe to call before `connect()`."""
    with _DB_LOCK:
        if cb not in _SUBSCRIBERS:
            _SUBSCRIBERS.append(cb)


def unsubscribe(cb) -> None:
    with _DB_LOCK:
        try:
            _SUBSCRIBERS.remove(cb)
        except ValueError:
            pass


def _notify_subscribers() -> None:
    for cb in list(_SUBSCRIBERS):
        try:
            cb()
        except Exception as e:
            print(f"[tasks] subscriber error: {e}")

TASK_STATUSES = {"planned", "running", "done", "failed",
                 "awaiting_user", "cancelled"}
STEP_STATUSES = {"planned", "ready", "running", "done", "failed",
                 "awaiting_user", "cancelled"}


def connect(db_path: Path | str) -> None:
    """Open or reuse the task database. Safe to call multiple times."""
    global _DB, _DB_PATH
    with _DB_LOCK:
        if _DB is not None and _DB_PATH == Path(db_path):
            return
        if _DB is not None:
            try:
                _DB.close()
            except Exception:
                pass
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        _DB = sqlite3.connect(
            str(db_path),
            check_same_thread=False,
            detect_types=sqlite3.PARSE_DECLTYPES,
        )
        _DB.row_factory = sqlite3.Row
        _DB.execute("PRAGMA journal_mode=WAL")
        _DB.execute("PRAGMA foreign_keys=ON")
        _DB_PATH = Path(db_path)
        _init_schema()


def _init_schema() -> None:
    assert _DB is not None
    _DB.executescript(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            goal          TEXT    NOT NULL,
            status        TEXT    NOT NULL DEFAULT 'planned',
            created_at    REAL    NOT NULL,
            updated_at    REAL    NOT NULL,
            result        TEXT    DEFAULT NULL,
            parent_id     INTEGER DEFAULT NULL,
            replan_count  INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS steps (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id      INTEGER NOT NULL,
            idx          INTEGER NOT NULL,
            verb         TEXT    NOT NULL,
            tool         TEXT    NOT NULL,
            args_json    TEXT    NOT NULL DEFAULT '{}',
            deps_json    TEXT    NOT NULL DEFAULT '[]',
            verify_json  TEXT    DEFAULT NULL,
            status       TEXT    NOT NULL DEFAULT 'planned',
            result       TEXT    DEFAULT NULL,
            error        TEXT    DEFAULT NULL,
            started_at   REAL    DEFAULT NULL,
            finished_at  REAL    DEFAULT NULL,
            FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_steps_task     ON steps(task_id);
        CREATE INDEX IF NOT EXISTS idx_steps_status   ON steps(status);
        CREATE INDEX IF NOT EXISTS idx_tasks_status   ON tasks(status);
        """
    )
    # Idempotent migrations: SQLite has no ADD COLUMN IF NOT EXISTS, so
    # each ALTER is wrapped and the duplicate-column error swallowed.
    for ddl in (
        "ALTER TABLE tasks ADD COLUMN replan_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE tasks ADD COLUMN idempotency_key TEXT DEFAULT NULL",
        "ALTER TABLE steps ADD COLUMN timeout_s INTEGER DEFAULT NULL",
        "ALTER TABLE steps ADD COLUMN duration_s REAL DEFAULT NULL",
    ):
        try:
            _DB.execute(ddl)
        except sqlite3.OperationalError:
            pass
    # Unique index on non-null keys; SQLite treats each NULL as distinct,
    # so unkeyed tasks are never considered duplicates of each other.
    _DB.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_idempotency "
        "ON tasks(idempotency_key) WHERE idempotency_key IS NOT NULL"
    )
    _DB.commit()


def _now() -> float:
    return time.time()


# ── Tasks ────────────────────────────────────────────────────────────────────

def create_task(goal: str, parent_id: int | None = None,
                idempotency_key: str | None = None) -> int:
    """Insert a task row. When `idempotency_key` is given and a task with the
    same key exists in any non-terminal or terminal state, return that task's
    id instead of creating a duplicate — this is how the agent layer avoids
    double-dispatching the same user request.
    """
    with _DB_LOCK:
        if idempotency_key:
            row = _DB.execute(
                "SELECT id FROM tasks WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if row:
                return int(row["id"])
        cur = _DB.execute(
            "INSERT INTO tasks(goal, status, created_at, updated_at, "
            "parent_id, idempotency_key) "
            "VALUES(?, 'planned', ?, ?, ?, ?)",
            (goal, _now(), _now(), parent_id, idempotency_key),
        )
        _DB.commit()
        new_id = int(cur.lastrowid)
    _notify_subscribers()
    return new_id


def set_task_status(task_id: int, status: str, result: str | None = None) -> None:
    if status not in TASK_STATUSES:
        raise ValueError(f"unknown task status: {status}")
    with _DB_LOCK:
        _DB.execute(
            "UPDATE tasks SET status=?, updated_at=?, "
            "result=COALESCE(?, result) WHERE id=?",
            (status, _now(), result, task_id),
        )
        _DB.commit()
    _notify_subscribers()


def get_task(task_id: int) -> dict | None:
    with _DB_LOCK:
        row = _DB.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return dict(row) if row else None


def list_tasks(status: str | Iterable[str] | None = None,
               limit: int = 50) -> list[dict]:
    with _DB_LOCK:
        if status is None:
            rows = _DB.execute(
                "SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            if isinstance(status, str):
                status = [status]
            placeholders = ",".join("?" * len(list(status)))
            rows = _DB.execute(
                f"SELECT * FROM tasks WHERE status IN ({placeholders}) "
                f"ORDER BY id DESC LIMIT ?",
                (*status, limit),
            ).fetchall()
        return [dict(r) for r in rows]


# ── Steps ────────────────────────────────────────────────────────────────────

def add_step(task_id: int, idx: int, verb: str, tool: str,
             args: dict | None = None, deps: list[int] | None = None,
             verify: dict | None = None,
             timeout_s: int | None = None) -> int:
    with _DB_LOCK:
        cur = _DB.execute(
            "INSERT INTO steps(task_id, idx, verb, tool, args_json, "
            "deps_json, verify_json, status, timeout_s) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, 'planned', ?)",
            (task_id, idx, verb, tool,
             json.dumps(args or {}),
             json.dumps(deps or []),
             json.dumps(verify) if verify else None,
             timeout_s),
        )
        _DB.commit()
        new_id = int(cur.lastrowid)
    _notify_subscribers()
    return new_id


def set_step_status(step_id: int, status: str,
                    result: str | None = None,
                    error: str | None = None) -> None:
    if status not in STEP_STATUSES:
        raise ValueError(f"unknown step status: {status}")
    now = _now()
    with _DB_LOCK:
        cols = ["status=?", "result=COALESCE(?, result)",
                "error=COALESCE(?, error)"]
        vals: list[Any] = [status, result, error]
        if status == "running":
            cols.append("started_at=?")
            vals.append(now)
        if status in ("done", "failed", "cancelled"):
            cols.append("finished_at=?")
            vals.append(now)
            # Fill duration_s from started_at when available, so every
            # terminal row carries a wall-clock measurement.
            row = _DB.execute(
                "SELECT started_at FROM steps WHERE id=?", (step_id,)
            ).fetchone()
            if row and row["started_at"] is not None:
                cols.append("duration_s=?")
                vals.append(max(0.0, now - float(row["started_at"])))
        vals.append(step_id)
        _DB.execute(f"UPDATE steps SET {', '.join(cols)} WHERE id=?", vals)
        _DB.commit()
    _notify_subscribers()


def get_step(step_id: int) -> dict | None:
    with _DB_LOCK:
        row = _DB.execute("SELECT * FROM steps WHERE id=?", (step_id,)).fetchone()
        return _hydrate_step(row)


def task_steps(task_id: int) -> list[dict]:
    with _DB_LOCK:
        rows = _DB.execute(
            "SELECT * FROM steps WHERE task_id=? ORDER BY idx", (task_id,)
        ).fetchall()
        return [_hydrate_step(r) for r in rows]


def ready_steps(task_id: int | None = None, limit: int = 32) -> list[dict]:
    """Return steps whose status is planned and whose deps are all done.
    Skips steps belonging to tasks in a non-running state (cancelled, etc)."""
    with _DB_LOCK:
        if task_id is None:
            rows = _DB.execute(
                "SELECT s.* FROM steps s JOIN tasks t ON s.task_id=t.id "
                "WHERE s.status='planned' AND t.status IN ('planned','running') "
                "ORDER BY s.id LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = _DB.execute(
                "SELECT * FROM steps WHERE task_id=? AND status='planned' "
                "ORDER BY idx LIMIT ?", (task_id, limit),
            ).fetchall()
        out: list[dict] = []
        for r in rows:
            s = _hydrate_step(r)
            deps = s["deps"]
            if not deps:
                out.append(s)
                continue
            placeholders = ",".join("?" * len(deps))
            done_count = _DB.execute(
                f"SELECT COUNT(*) FROM steps WHERE id IN ({placeholders}) "
                f"AND status='done'",
                deps,
            ).fetchone()[0]
            if done_count == len(deps):
                out.append(s)
        return out


def cancel_task(task_id: int) -> None:
    with _DB_LOCK:
        _DB.execute(
            "UPDATE steps SET status='cancelled', finished_at=? "
            "WHERE task_id=? AND status IN ('planned','ready','running','awaiting_user')",
            (_now(), task_id),
        )
        _DB.execute(
            "UPDATE tasks SET status='cancelled', updated_at=? WHERE id=?",
            (_now(), task_id),
        )
        _DB.commit()
    _notify_subscribers()


def cancel_pending_steps(task_id: int, except_ids: Iterable[int] | None = None) -> int:
    """Cancel only not-yet-started steps in a task; preserves done/failed/running.

    Used by the replanner: when a step fails and new steps replace the remaining
    work, the old planned/ready steps have to go so they don't run against a
    stale plan. Returns the number of steps cancelled."""
    excludes = list(except_ids or [])
    with _DB_LOCK:
        if excludes:
            ph = ",".join("?" * len(excludes))
            cur = _DB.execute(
                f"UPDATE steps SET status='cancelled', finished_at=? "
                f"WHERE task_id=? AND status IN ('planned','ready') "
                f"AND id NOT IN ({ph})",
                (_now(), task_id, *excludes),
            )
        else:
            cur = _DB.execute(
                "UPDATE steps SET status='cancelled', finished_at=? "
                "WHERE task_id=? AND status IN ('planned','ready')",
                (_now(), task_id),
            )
        _DB.commit()
        return int(cur.rowcount or 0)


def get_replan_count(task_id: int) -> int:
    with _DB_LOCK:
        row = _DB.execute(
            "SELECT replan_count FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        return int(row[0]) if row else 0


def bump_replan_count(task_id: int) -> int:
    with _DB_LOCK:
        _DB.execute(
            "UPDATE tasks SET replan_count=replan_count+1, updated_at=? WHERE id=?",
            (_now(), task_id),
        )
        _DB.commit()
    return get_replan_count(task_id)


def next_step_idx(task_id: int) -> int:
    """Highest step idx currently on the task plus one. Used when the
    replanner splices additional steps into an existing task so idx values
    stay monotonic."""
    with _DB_LOCK:
        row = _DB.execute(
            "SELECT COALESCE(MAX(idx), 0) FROM steps WHERE task_id=?",
            (task_id,),
        ).fetchone()
        return int(row[0] or 0) + 1


def _hydrate_step(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    d["args"]   = json.loads(d.pop("args_json")   or "{}")
    d["deps"]   = json.loads(d.pop("deps_json")   or "[]")
    vj = d.pop("verify_json")
    d["verify"] = json.loads(vj) if vj else None
    return d


# ── Convenience ─────────────────────────────────────────────────────────────

def task_summary(task_id: int) -> str:
    """One-line human summary of a task and its step progress."""
    t = get_task(task_id)
    if not t:
        return f"Task {task_id}: not found."
    steps = task_steps(task_id)
    counts: dict[str, int] = {}
    for s in steps:
        counts[s["status"]] = counts.get(s["status"], 0) + 1
    parts = [f"#{t['id']} {t['status']}"]
    if steps:
        parts.append(" / ".join(f"{k}={v}" for k, v in counts.items()))
    parts.append(f"goal: {t['goal'][:80]}")
    return " · ".join(parts)


def pending_task_count() -> int:
    with _DB_LOCK:
        return int(_DB.execute(
            "SELECT COUNT(*) FROM tasks WHERE status IN ('planned','running','awaiting_user')"
        ).fetchone()[0])
