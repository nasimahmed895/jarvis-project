"""
Project action — projects, milestones, and project tasks.

Wraps ``core/projects.py``. Separate from ``actions/todo.py`` (flat items)
and from the agent-worker task store — this is for user-facing *projects*
with lifecycle, milestones, and action items underneath.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from core import projects as _p


_CONNECTED = False


def _ensure() -> None:
    global _CONNECTED
    if _CONNECTED:
        return
    db = Path(__file__).resolve().parent.parent / "memory" / "projects.sqlite"
    _p.connect(db)
    _CONNECTED = True


def _int(raw, default=None):
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _fmt_project(p: dict) -> str:
    bits = [f"#{p['id']} [{p['status']}] {p['name']}"]
    if p.get("owner"):
        bits.append(f"· owner={p['owner']}")
    if p.get("target_date"):
        bits.append(f"· target={p['target_date']}")
    return " ".join(bits)


def _fmt_milestone(m: dict) -> str:
    return (f"  #{m['id']} [{m['status']}] {m['name']}"
            + (f" · target {m['target_date']}" if m.get("target_date") else ""))


def _fmt_task(t: dict) -> str:
    bits = [f"  #{t['id']} [{t['status']}] {t['text']}"]
    if t.get("owner"):
        bits.append(f"· owner={t['owner']}")
    if t.get("due_date"):
        bits.append(f"· due {t['due_date']}")
    return " ".join(bits)


def project(
    parameters:    dict | None = None,
    response: Any = None,
    player:   Any = None,
    session_memory: Any = None,
) -> str:
    """Manage user-facing projects: create, update, list, summarise.

    Actions:
      add               name= description= owner= status= start_date= target_date=
      update            id= [name= description= owner= status= start_date= target_date=]
      set_status        id= status=<planning|active|on_hold|done|cancelled>
      list              status= (filter)
      get               id=
      search            query=
      add_milestone     project_id= name= target_date= notes=
      milestone_status  id= status=<pending|done|slipped|cancelled>
      list_milestones   project_id=
      add_task          project_id= text= milestone_id= owner= due_date=
      task_status       id= status=<open|done|blocked|cancelled>
      list_tasks        project_id= status=
      summary           id=
    """
    _ensure()
    p = parameters or {}
    action = str(p.get("action", "")).lower().strip()

    if action == "add":
        name = str(p.get("name") or "").strip()
        if not name:
            return "project add: provide 'name'."
        try:
            pid = _p.add_project(
                name,
                description=p.get("description") or None,
                owner=p.get("owner") or None,
                status=str(p.get("status") or "planning"),
                start_date=p.get("start_date") or None,
                target_date=p.get("target_date") or None,
            )
        except ValueError as e:
            return f"project add: {e}"
        return f"Created project #{pid}: {name}"

    if action == "update":
        pid = _int(p.get("id"))
        if pid is None:
            return "project update: provide numeric 'id'."
        patch = {k: p[k] for k in
                 ("name", "description", "owner", "status",
                  "start_date", "target_date", "actual_end")
                 if k in p and p[k] is not None}
        if not patch:
            return "project update: nothing to update."
        return (f"Updated project #{pid}."
                if _p.update_project(pid, **patch)
                else f"Project #{pid} not found.")

    if action == "set_status":
        pid = _int(p.get("id"))
        status = str(p.get("status") or "").lower()
        if pid is None or status not in _p.PROJECT_STATUSES:
            return (f"project set_status: provide 'id' and status in "
                    f"{', '.join(_p.PROJECT_STATUSES)}.")
        return (f"Project #{pid} → {status}."
                if _p.set_project_status(pid, status)
                else f"Project #{pid} not found.")

    if action == "list":
        rows = _p.list_projects(status=p.get("status") or None)
        if not rows:
            return "No projects."
        return (f"Projects ({len(rows)}):\n"
                + "\n".join(_fmt_project(r) for r in rows))

    if action == "get":
        pid = _int(p.get("id"))
        if pid is None:
            return "project get: provide numeric 'id'."
        proj = _p.get_project(pid)
        if not proj:
            return f"Project #{pid} not found."
        lines = [_fmt_project(proj)]
        if proj.get("description"):
            lines.append(f"Description: {proj['description']}")
        for k in ("start_date", "target_date", "actual_end"):
            if proj.get(k):
                lines.append(f"{k}: {proj[k]}")
        return "\n".join(lines)

    if action == "search":
        q = str(p.get("query") or "")
        rows = _p.search_projects(q)
        if not rows:
            return f"No projects matching {q!r}."
        return (f"Matches for {q!r}:\n"
                + "\n".join(_fmt_project(r) for r in rows))

    if action == "add_milestone":
        pid = _int(p.get("project_id"))
        name = str(p.get("name") or "").strip()
        if pid is None or not name:
            return "project add_milestone: provide 'project_id' and 'name'."
        try:
            mid = _p.add_milestone(
                pid, name,
                target_date=p.get("target_date") or None,
                notes=p.get("notes") or None,
            )
        except ValueError as e:
            return f"project add_milestone: {e}"
        return f"Milestone #{mid} added to project #{pid}."

    if action == "milestone_status":
        mid = _int(p.get("id"))
        status = str(p.get("status") or "").lower()
        if mid is None or status not in _p.MILESTONE_STATUSES:
            return (f"project milestone_status: provide 'id' and status in "
                    f"{', '.join(_p.MILESTONE_STATUSES)}.")
        return (f"Milestone #{mid} → {status}."
                if _p.set_milestone_status(mid, status)
                else f"Milestone #{mid} not found.")

    if action == "list_milestones":
        pid = _int(p.get("project_id"))
        if pid is None:
            return "project list_milestones: provide 'project_id'."
        rows = _p.list_milestones(pid)
        if not rows:
            return f"No milestones on project #{pid}."
        return (f"Milestones on #{pid}:\n"
                + "\n".join(_fmt_milestone(m) for m in rows))

    if action == "add_task":
        pid = _int(p.get("project_id"))
        text = str(p.get("text") or "").strip()
        if pid is None or not text:
            return "project add_task: provide 'project_id' and 'text'."
        try:
            tid = _p.add_project_task(
                pid, text,
                milestone_id=_int(p.get("milestone_id")),
                owner=p.get("owner") or None,
                due_date=p.get("due_date") or None,
            )
        except ValueError as e:
            return f"project add_task: {e}"
        return f"Task #{tid} added to project #{pid}."

    if action == "task_status":
        tid = _int(p.get("id"))
        status = str(p.get("status") or "").lower()
        if tid is None or status not in _p.TASK_STATUSES:
            return (f"project task_status: provide 'id' and status in "
                    f"{', '.join(_p.TASK_STATUSES)}.")
        return (f"Task #{tid} → {status}."
                if _p.set_project_task_status(tid, status)
                else f"Task #{tid} not found.")

    if action == "list_tasks":
        pid = _int(p.get("project_id"))
        if pid is None:
            return "project list_tasks: provide 'project_id'."
        rows = _p.list_project_tasks(pid, status=p.get("status") or None)
        if not rows:
            return f"No tasks on project #{pid}."
        return (f"Tasks on #{pid}:\n"
                + "\n".join(_fmt_task(t) for t in rows))

    if action == "summary":
        pid = _int(p.get("id"))
        if pid is None:
            return "project summary: provide 'id'."
        s = _p.project_summary(pid)
        if not s:
            return f"Project #{pid} not found."
        proj = s["project"]
        lines = [_fmt_project(proj),
                 f"Completion: {s['completion_percent']}% "
                 f"({s['task_counts'].get('done', 0)}/{s['total_tasks']} tasks)"]
        if s["milestone_counts"]:
            lines.append("Milestones: " + ", ".join(
                f"{k}={v}" for k, v in s["milestone_counts"].items()
            ))
        if s["next_milestone"]:
            nm = s["next_milestone"]
            lines.append(f"Next milestone: {nm['name']} "
                         f"(target {nm.get('target_date','?')})")
        return "\n".join(lines)

    return (f"project: unknown action {action!r}. "
            "Actions: add | update | set_status | list | get | search | "
            "add_milestone | milestone_status | list_milestones | "
            "add_task | task_status | list_tasks | summary")


TOOL = {
    "name": "project",
    "description": (
        "Manage user-facing projects (distinct from flat todos). Each "
        "project has a lifecycle (planning → active → on_hold → done | "
        "cancelled), optional owner, target date, milestones, and "
        "action items. Use 'add' when the user starts a new project; "
        "'add_milestone' and 'add_task' nest work under it; 'summary' "
        "returns a one-paragraph progress digest. Keeping projects "
        "separate from todos is the point: 'buy milk' is a todo, "
        "'launch Q1 campaign' is a project."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action":       {"type": "STRING", "description": "see docstring"},
            "id":           {"type": "INTEGER", "description": "Row id (project/milestone/task depending on action)."},
            "project_id":   {"type": "INTEGER", "description": "Parent project id for milestones/tasks."},
            "milestone_id": {"type": "INTEGER", "description": "Parent milestone id for a task (optional)."},
            "name":         {"type": "STRING",  "description": "Project or milestone name."},
            "text":         {"type": "STRING",  "description": "Project task text."},
            "description":  {"type": "STRING",  "description": "Project description."},
            "owner":        {"type": "STRING",  "description": "Owner/assignee."},
            "status":       {"type": "STRING",  "description": "Status (project/milestone/task)."},
            "start_date":   {"type": "STRING",  "description": "Project start date YYYY-MM-DD."},
            "target_date":  {"type": "STRING",  "description": "Project or milestone target date YYYY-MM-DD."},
            "actual_end":   {"type": "STRING",  "description": "Project actual end date YYYY-MM-DD (when closing)."},
            "due_date":     {"type": "STRING",  "description": "Project task due date YYYY-MM-DD."},
            "notes":        {"type": "STRING",  "description": "Milestone notes."},
            "query":        {"type": "STRING",  "description": "Substring for 'search'."},
        },
        "required": ["action"],
    },
    "handler": project,
}
