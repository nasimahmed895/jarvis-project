"""
actions/habit.py — habit tracking (P3.1).

Habits sit beside tasks in the productivity stack: a task has a one-shot
outcome, a habit has a cadence and a streak. Stored in its own SQLite file
(`habits.sqlite`) under the memory directory so uninstall + backup is
symmetric with the other stores.

Core operations (exposed via the single `habit` tool):
    add             — register a new habit
    mark_done       — record completion today (idempotent per day)
    list            — enumerate active habits with streak + due state
    remove          — delete a habit
    streak          — read a habit's current streak

Cadence grammar: `daily` | `weekly` | `every:N` (N days). The streak
calculation walks backwards from today/this-week and counts contiguous
completions. A single missed window resets the streak on the next
mark_done — not on time passing, so a user who glances at the list
sees the old streak until they explicitly log the next completion.
"""
from __future__ import annotations

import datetime as _dt
import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional

from memory.memory_manager import BASE_DIR
from core.logging_setup import get_logger

_log = get_logger(__name__)

_DB_PATH = BASE_DIR / "memory" / "habits.sqlite"
_LOCK = threading.RLock()
_CONN: sqlite3.Connection | None = None

_CADENCES = {"daily", "weekly"}


def _conn() -> sqlite3.Connection:
    global _CONN
    with _LOCK:
        if _CONN is None:
            _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            _CONN = sqlite3.connect(
                str(_DB_PATH), check_same_thread=False,
                detect_types=sqlite3.PARSE_DECLTYPES,
            )
            _CONN.row_factory = sqlite3.Row
            _CONN.execute("PRAGMA journal_mode=WAL")
            _CONN.executescript("""
                CREATE TABLE IF NOT EXISTS habits (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    name        TEXT    NOT NULL UNIQUE,
                    cadence     TEXT    NOT NULL,
                    created_at  TEXT    NOT NULL
                );
                CREATE TABLE IF NOT EXISTS habit_log (
                    habit_id    INTEGER NOT NULL,
                    done_at     TEXT    NOT NULL,
                    PRIMARY KEY (habit_id, done_at),
                    FOREIGN KEY (habit_id) REFERENCES habits(id) ON DELETE CASCADE
                );
            """)
            _CONN.commit()
        return _CONN


def _reset_connection_for_tests(path: Path | None = None) -> None:
    """Testing helper — rebind the module to a fresh DB."""
    global _CONN, _DB_PATH
    with _LOCK:
        if _CONN is not None:
            try:
                _CONN.close()
            except Exception:
                pass
            _CONN = None
        if path is not None:
            _DB_PATH = path


# ── Cadence parsing ──────────────────────────────────────────────────────────

def _validate_cadence(cadence: str) -> str:
    c = (cadence or "").strip().lower()
    if c in _CADENCES:
        return c
    if c.startswith("every:"):
        try:
            n = int(c.split(":", 1)[1])
        except ValueError:
            raise ValueError(f"every:N requires an integer, got {cadence!r}")
        if n <= 0:
            raise ValueError("every:N must be positive")
        return f"every:{n}"
    raise ValueError(f"cadence must be daily / weekly / every:N, got {cadence!r}")


def _window_key(cadence: str, day: _dt.date) -> str:
    """Return the string a completion is recorded under, so idempotency per
    cadence slot is a simple PRIMARY KEY check."""
    if cadence == "daily" or cadence.startswith("every:"):
        return day.isoformat()
    if cadence == "weekly":
        year, week, _ = day.isocalendar()
        return f"{year}-W{week:02d}"
    raise ValueError(cadence)


def _streak_length(cadence: str, done_at: list[str], today: _dt.date) -> int:
    """Count contiguous completed windows ending at (or before) today."""
    if not done_at:
        return 0
    completed = set(done_at)
    streak = 0
    if cadence == "daily":
        cursor = today
        # Allow today-not-yet-logged by walking back one day if missing.
        if cursor.isoformat() not in completed:
            cursor -= _dt.timedelta(days=1)
        while cursor.isoformat() in completed:
            streak += 1
            cursor -= _dt.timedelta(days=1)
        return streak
    if cadence.startswith("every:"):
        step = int(cadence.split(":", 1)[1])
        cursor = today
        if cursor.isoformat() not in completed:
            cursor -= _dt.timedelta(days=step)
        while cursor.isoformat() in completed:
            streak += 1
            cursor -= _dt.timedelta(days=step)
        return streak
    if cadence == "weekly":
        y, w, _ = today.isocalendar()
        if f"{y}-W{w:02d}" not in completed:
            # back one week
            prior = today - _dt.timedelta(days=7)
            y, w, _ = prior.isocalendar()
        while f"{y}-W{w:02d}" in completed:
            streak += 1
            prior = _dt.date.fromisocalendar(y, w, 1) - _dt.timedelta(days=7)
            y, w, _ = prior.isocalendar()
        return streak
    return 0


# ── Operations ───────────────────────────────────────────────────────────────

def add(name: str, cadence: str, now: Optional[_dt.date] = None) -> dict:
    name = (name or "").strip()
    if not name:
        raise ValueError("habit name is required")
    normalized = _validate_cadence(cadence)
    today = (now or _dt.date.today()).isoformat()
    with _LOCK:
        cur = _conn().execute(
            "INSERT INTO habits(name, cadence, created_at) VALUES(?, ?, ?)",
            (name, normalized, today),
        )
        _conn().commit()
        hid = int(cur.lastrowid)
    return {"id": hid, "name": name, "cadence": normalized}


def mark_done(name: str, on: Optional[_dt.date] = None) -> dict:
    habit = _lookup(name)
    if not habit:
        raise KeyError(f"no habit named {name!r}")
    day = on or _dt.date.today()
    slot = _window_key(habit["cadence"], day)
    with _LOCK:
        try:
            _conn().execute(
                "INSERT INTO habit_log(habit_id, done_at) VALUES(?, ?)",
                (habit["id"], slot),
            )
            _conn().commit()
            created = True
        except sqlite3.IntegrityError:
            created = False
    return {
        "habit": habit["name"],
        "slot":  slot,
        "new":   created,
        "streak": _streak_for(habit, day),
    }


def remove(name: str) -> dict:
    habit = _lookup(name)
    if not habit:
        return {"removed": False}
    with _LOCK:
        _conn().execute("DELETE FROM habits WHERE id=?", (habit["id"],))
        _conn().commit()
    return {"removed": True, "habit": habit["name"]}


def streak(name: str, on: Optional[_dt.date] = None) -> int:
    habit = _lookup(name)
    if not habit:
        raise KeyError(f"no habit named {name!r}")
    return _streak_for(habit, on or _dt.date.today())


def list_habits(on: Optional[_dt.date] = None) -> list[dict]:
    day = on or _dt.date.today()
    with _LOCK:
        rows = _conn().execute(
            "SELECT * FROM habits ORDER BY name"
        ).fetchall()
    out = []
    for r in rows:
        done = _done_slots(r["id"])
        slot = _window_key(r["cadence"], day)
        out.append({
            "id":         r["id"],
            "name":       r["name"],
            "cadence":    r["cadence"],
            "streak":     _streak_length(r["cadence"], done, day),
            "due_today":  slot not in done,
            "last_slot":  (sorted(done)[-1] if done else None),
            "created_at": r["created_at"],
        })
    return out


# ── Internals ────────────────────────────────────────────────────────────────

def _lookup(name: str) -> dict | None:
    with _LOCK:
        r = _conn().execute(
            "SELECT * FROM habits WHERE name=?", (name,)
        ).fetchone()
        return dict(r) if r else None


def _done_slots(habit_id: int) -> list[str]:
    with _LOCK:
        rows = _conn().execute(
            "SELECT done_at FROM habit_log WHERE habit_id=? ORDER BY done_at",
            (habit_id,),
        ).fetchall()
        return [r["done_at"] for r in rows]


def _streak_for(habit: dict, day: _dt.date) -> int:
    return _streak_length(habit["cadence"], _done_slots(habit["id"]), day)


# ── Tool dispatch ────────────────────────────────────────────────────────────

def _tool_entry(parameters: dict, **_kw: Any) -> str:
    action = (parameters.get("action") or "").lower()
    if action == "add":
        try:
            info = add(parameters.get("name", ""), parameters.get("cadence", ""))
        except ValueError as e:
            return str(e)
        return f"Added habit '{info['name']}' ({info['cadence']})."
    if action == "mark_done":
        try:
            info = mark_done(parameters.get("name", ""))
        except KeyError as e:
            return str(e)
        if info["new"]:
            return f"✓ {info['habit']} — streak now {info['streak']}."
        return f"Already marked done for this slot. Streak is {info['streak']}."
    if action == "list":
        habits = list_habits()
        if not habits:
            return "No habits tracked yet."
        lines = [f"Tracking {len(habits)} habit(s):"]
        for h in habits:
            marker = "⏳" if h["due_today"] else "✓"
            lines.append(f"  {marker} {h['name']} ({h['cadence']}) — streak {h['streak']}")
        return "\n".join(lines)
    if action == "remove":
        info = remove(parameters.get("name", ""))
        return f"Removed '{parameters.get('name')}'." if info["removed"] else "No such habit."
    if action == "streak":
        try:
            s = streak(parameters.get("name", ""))
        except KeyError as e:
            return str(e)
        return f"{parameters.get('name')}: streak = {s}"
    return "Unknown action. Use: add | mark_done | list | remove | streak."


TOOL = {
    "name": "habit",
    "description": (
        "Track recurring habits. Cadences: daily, weekly, every:N (every N "
        "days). Each `mark_done` is idempotent per slot. `list` shows "
        "current streaks and whether each habit is due in the current slot."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action":  {"type": "string",
                        "enum": ["add", "mark_done", "list", "remove", "streak"]},
            "name":    {"type": "string"},
            "cadence": {"type": "string",
                        "description": "daily | weekly | every:N"},
        },
        "required": ["action"],
    },
    "run": _tool_entry,
}
