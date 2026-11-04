"""
Project management store.

Fills the section-4 "Project Management" gap identified in the audit.
Distinct from:

  * ``actions/todo.py`` — flat per-user items, no grouping or lifecycle
  * ``core/tasks.py``   — JARVIS's own internal plans, machine-driven

Projects here are user-facing. Each project has a lifecycle (planning →
active → on_hold → done | cancelled), optional owner, target date,
milestones, and action items. Milestones have target dates and statuses;
action items are granular todos nested under the project.

Everything local; schema matches the pattern used by CRM and finance so a
reader who knows one knows all three.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Iterable

_LOCK = threading.RLock()
_DB: sqlite3.Connection | None = None
_DB_PATH: Path | None = None


PROJECT_STATUSES = ("planning", "active", "on_hold", "done", "cancelled")
MILESTONE_STATUSES = ("pending", "done", "slipped", "cancelled")
TASK_STATUSES = ("open", "done", "blocked", "cancelled")


def connect(db_path: Path | str) -> None:
    global _DB, _DB_PATH
    with _LOCK:
        want = Path(db_path)
        if _DB is not None and _DB_PATH == want:
            return
        if _DB is not None:
            try:
                _DB.close()
            except Exception:
                pass
        want.parent.mkdir(parents=True, exist_ok=True)
        _DB = sqlite3.connect(str(want), check_same_thread=False)
        _DB.row_factory = sqlite3.Row
        _DB.execute("PRAGMA journal_mode=WAL")
        _DB.execute("PRAGMA foreign_keys=ON")
        _DB_PATH = want
        _init_schema()


def _init_schema() -> None:
    assert _DB is not None
    _DB.executescript(
        """
        CREATE TABLE IF NOT EXISTS projects (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            name         TEXT    NOT NULL,
            description  TEXT    DEFAULT NULL,
            status       TEXT    NOT NULL DEFAULT 'planning',
            owner        TEXT    DEFAULT NULL,
            start_date   TEXT    DEFAULT NULL,
            target_date  TEXT    DEFAULT NULL,
            actual_end   TEXT    DEFAULT NULL,
            created_at   REAL    NOT NULL,
            updated_at   REAL    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_proj_status ON projects(status);

        CREATE TABLE IF NOT EXISTS milestones (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id   INTEGER NOT NULL,
            name         TEXT    NOT NULL,
            target_date  TEXT    DEFAULT NULL,
            status       TEXT    NOT NULL DEFAULT 'pending',
            notes        TEXT    DEFAULT NULL,
            created_at   REAL    NOT NULL,
            done_at      REAL    DEFAULT NULL,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_ms_project ON milestones(project_id);
        CREATE INDEX IF NOT EXISTS idx_ms_status  ON milestones(status);

        CREATE TABLE IF NOT EXISTS project_tasks (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id   INTEGER NOT NULL,
            milestone_id INTEGER DEFAULT NULL,
            text         TEXT    NOT NULL,
            status       TEXT    NOT NULL DEFAULT 'open',
            owner        TEXT    DEFAULT NULL,
            due_date     TEXT    DEFAULT NULL,
            created_at   REAL    NOT NULL,
            done_at      REAL    DEFAULT NULL,
            FOREIGN KEY (project_id)   REFERENCES projects(id)   ON DELETE CASCADE,
            FOREIGN KEY (milestone_id) REFERENCES milestones(id) ON DELETE SET NULL
        );
        CREATE INDEX IF NOT EXISTS idx_ptasks_project ON project_tasks(project_id);
        CREATE INDEX IF NOT EXISTS idx_ptasks_status  ON project_tasks(status);
        """
    )
    _DB.commit()


def _now() -> float:
    return time.time()


def _norm(val: str | None, allowed: tuple[str, ...], default: str) -> str:
    v = (val or "").strip().lower()
    return v if v in allowed else default


# ── Projects ────────────────────────────────────────────────────────────────

def add_project(name: str, *, description: str | None = None,
                owner: str | None = None,
                status: str = "planning",
                start_date: str | None = None,
                target_date: str | None = None) -> int:
    if not name.strip():
        raise ValueError("project name required")
    now = _now()
    with _LOCK:
        cur = _DB.execute(
            "INSERT INTO projects(name, description, status, owner, "
            "start_date, target_date, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (name.strip(), description,
             _norm(status, PROJECT_STATUSES, "planning"),
             owner, start_date, target_date, now, now),
        )
        _DB.commit()
        return int(cur.lastrowid)


def update_project(project_id: int, **fields) -> bool:
    allowed = {"name", "description", "status", "owner", "start_date",
               "target_date", "actual_end"}
    sets: list[str] = []
    vals: list = []
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k == "status":
            v = _norm(v, PROJECT_STATUSES, "planning")
        sets.append(f"{k}=?")
        vals.append(v)
    if not sets:
        return False
    sets.append("updated_at=?")
    vals.append(_now())
    vals.append(project_id)
    with _LOCK:
        res = _DB.execute(
            f"UPDATE projects SET {', '.join(sets)} WHERE id=?", vals
        )
        _DB.commit()
    return bool(res.rowcount)


def set_project_status(project_id: int, status: str) -> bool:
    status = _norm(status, PROJECT_STATUSES, "planning")
    extra: dict = {"status": status}
    if status == "done":
        extra["actual_end"] = datetime.now().strftime("%Y-%m-%d")
    return update_project(project_id, **extra)


def get_project(project_id: int) -> dict | None:
    with _LOCK:
        row = _DB.execute(
            "SELECT * FROM projects WHERE id=?", (project_id,)
        ).fetchone()
    return dict(row) if row else None


def list_projects(status: str | Iterable[str] | None = None,
                  limit: int = 50) -> list[dict]:
    with _LOCK:
        if status is None:
            rows = _DB.execute(
                "SELECT * FROM projects ORDER BY status, name LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            if isinstance(status, str):
                status = [status]
            vals = [_norm(s, PROJECT_STATUSES, "planning") for s in status]
            ph = ",".join("?" * len(vals))
            rows = _DB.execute(
                f"SELECT * FROM projects WHERE status IN ({ph}) "
                f"ORDER BY name LIMIT ?",
                (*vals, limit),
            ).fetchall()
    return [dict(r) for r in rows]


def find_project(name: str) -> dict | None:
    """Case-insensitive exact match on name. For partial matching the
    caller should use ``search_projects``."""
    with _LOCK:
        row = _DB.execute(
            "SELECT * FROM projects WHERE LOWER(name)=? LIMIT 1",
            (name.strip().lower(),),
        ).fetchone()
    return dict(row) if row else None


def search_projects(query: str, limit: int = 20) -> list[dict]:
    q = f"%{(query or '').strip().lower()}%"
    if q == "%%":
        return list_projects(limit=limit)
    with _LOCK:
        rows = _DB.execute(
            "SELECT * FROM projects WHERE "
            "LOWER(name) LIKE ? OR LOWER(description) LIKE ? "
            "ORDER BY name LIMIT ?",
            (q, q, limit),
        ).fetchall()
    return [dict(r) for r in rows]


# ── Milestones ──────────────────────────────────────────────────────────────

def add_milestone(project_id: int, name: str, *,
                  target_date: str | None = None,
                  notes: str | None = None) -> int:
    if not name.strip():
        raise ValueError("milestone name required")
    with _LOCK:
        cur = _DB.execute(
            "INSERT INTO milestones(project_id, name, target_date, notes, "
            "created_at) VALUES(?,?,?,?,?)",
            (project_id, name.strip(), target_date, notes, _now()),
        )
        _DB.commit()
        return int(cur.lastrowid)


def set_milestone_status(milestone_id: int, status: str) -> bool:
    status = _norm(status, MILESTONE_STATUSES, "pending")
    with _LOCK:
        cols = ["status=?"]
        vals: list = [status]
        if status == "done":
            cols.append("done_at=?")
            vals.append(_now())
        vals.append(milestone_id)
        res = _DB.execute(
            f"UPDATE milestones SET {', '.join(cols)} WHERE id=?", vals
        )
        _DB.commit()
    return bool(res.rowcount)


def list_milestones(project_id: int) -> list[dict]:
    with _LOCK:
        rows = _DB.execute(
            "SELECT * FROM milestones WHERE project_id=? "
            "ORDER BY COALESCE(target_date, '9999-12-31'), id",
            (project_id,),
        ).fetchall()
    return [dict(r) for r in rows]


# ── Project tasks ───────────────────────────────────────────────────────────

def add_project_task(project_id: int, text: str, *,
                     milestone_id: int | None = None,
                     owner: str | None = None,
                     due_date: str | None = None) -> int:
    if not text.strip():
        raise ValueError("project task text required")
    with _LOCK:
        cur = _DB.execute(
            "INSERT INTO project_tasks(project_id, milestone_id, text, "
            "owner, due_date, created_at) VALUES(?,?,?,?,?,?)",
            (project_id, milestone_id, text.strip(), owner,
             due_date, _now()),
        )
        _DB.commit()
        return int(cur.lastrowid)


def set_project_task_status(task_id: int, status: str) -> bool:
    status = _norm(status, TASK_STATUSES, "open")
    with _LOCK:
        cols = ["status=?"]
        vals: list = [status]
        if status == "done":
            cols.append("done_at=?")
            vals.append(_now())
        vals.append(task_id)
        res = _DB.execute(
            f"UPDATE project_tasks SET {', '.join(cols)} WHERE id=?", vals
        )
        _DB.commit()
    return bool(res.rowcount)


def list_project_tasks(project_id: int,
                       status: str | Iterable[str] | None = None
                       ) -> list[dict]:
    with _LOCK:
        if status is None:
            rows = _DB.execute(
                "SELECT * FROM project_tasks WHERE project_id=? "
                "ORDER BY status, COALESCE(due_date, '9999-12-31'), id",
                (project_id,),
            ).fetchall()
        else:
            if isinstance(status, str):
                status = [status]
            vals = [_norm(s, TASK_STATUSES, "open") for s in status]
            ph = ",".join("?" * len(vals))
            rows = _DB.execute(
                f"SELECT * FROM project_tasks WHERE project_id=? AND "
                f"status IN ({ph}) "
                f"ORDER BY COALESCE(due_date, '9999-12-31'), id",
                (project_id, *vals),
            ).fetchall()
    return [dict(r) for r in rows]


# ── Project rollup ──────────────────────────────────────────────────────────

def project_summary(project_id: int) -> dict:
    """Aggregate stats for one project: counts per status for tasks and
    milestones, next upcoming milestone, overall completion fraction."""
    proj = get_project(project_id)
    if not proj:
        return {}
    tasks = list_project_tasks(project_id)
    mstones = list_milestones(project_id)
    t_counts: dict[str, int] = {}
    for t in tasks:
        t_counts[t["status"]] = t_counts.get(t["status"], 0) + 1
    m_counts: dict[str, int] = {}
    for m in mstones:
        m_counts[m["status"]] = m_counts.get(m["status"], 0) + 1

    today = datetime.now().strftime("%Y-%m-%d")
    next_milestone = next(
        (m for m in mstones
         if m["status"] == "pending" and (m["target_date"] or "") >= today),
        None,
    )

    done_tasks = t_counts.get("done", 0)
    total_tasks = sum(t_counts.values())
    pct = (done_tasks / total_tasks) if total_tasks else 0.0

    return {
        "project":            dict(proj),
        "task_counts":        t_counts,
        "milestone_counts":   m_counts,
        "next_milestone":     next_milestone,
        "completion_percent": round(pct * 100, 1),
        "total_tasks":        total_tasks,
    }
