"""
Todo / shopping / notes manager.

Local SQLite-backed task-and-list store that is distinct from the agent
worker's internal tasks table. Those are JARVIS's own plans; these are the
USER's — "buy milk", "call mum", "finish quarterly report". Lists are
free-form tags so "shopping", "errands", "work", "ideas" all coexist in
one table without extra schema work.

Every action here is a local disk write, so no permission gates — a user
asking JARVIS to remember a todo has already consented. Nothing phones home.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


_LOCK = threading.RLock()
_DB: sqlite3.Connection | None = None
_DB_PATH: Path | None = None
_DEFAULT_LIST = "todo"


def _db_path() -> Path:
    from pathlib import Path as _P
    base = _P(__file__).resolve().parent.parent / "memory" / "todos.sqlite"
    base.parent.mkdir(parents=True, exist_ok=True)
    return base


def _connect() -> None:
    global _DB, _DB_PATH
    with _LOCK:
        want = _db_path()
        if _DB is not None and _DB_PATH == want:
            return
        _DB = sqlite3.connect(str(want), check_same_thread=False)
        _DB.row_factory = sqlite3.Row
        _DB.execute("PRAGMA journal_mode=WAL")
        _DB_PATH = want
        _DB.executescript(
            """
            CREATE TABLE IF NOT EXISTS todos (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                list        TEXT    NOT NULL DEFAULT 'todo',
                text        TEXT    NOT NULL,
                done        INTEGER NOT NULL DEFAULT 0,
                due         TEXT    DEFAULT NULL,
                priority    INTEGER NOT NULL DEFAULT 0,
                created_at  REAL    NOT NULL,
                done_at     REAL    DEFAULT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_todos_list ON todos(list);
            CREATE INDEX IF NOT EXISTS idx_todos_done ON todos(done);
            """
        )
        _DB.commit()


def _now() -> float:
    return time.time()


def _fmt(row: sqlite3.Row) -> str:
    d = dict(row)
    mark = "✓" if d["done"] else "·"
    due = f" (due {d['due']})" if d.get("due") else ""
    pr  = "!" * max(0, int(d.get("priority") or 0))
    return f"#{d['id']} {mark} [{d['list']}] {pr}{d['text']}{due}"


def todo(
    parameters:    dict | None = None,
    response: Any = None,
    player:   Any = None,
    session_memory: Any = None,
) -> str:
    """Manage lists of user todos / shopping items / notes.

    Actions:
      add     text=<item> list=<tag> due=<YYYY-MM-DD> priority=<0-3>
      list    list=<tag> status=<open|done|all>
      done    id=<n>  — mark complete
      undo    id=<n>  — mark incomplete again
      delete  id=<n>
      search  query=<substring>
      clear   list=<tag>  — delete every row in a list (asks confirm via trust)
      lists   — show list names + counts
    """
    _connect()
    params = parameters or {}
    action = str(params.get("action", "list")).lower().strip()
    lst    = str(params.get("list") or _DEFAULT_LIST).lower().strip() or _DEFAULT_LIST
    assert _DB is not None

    with _LOCK:
        if action == "add":
            text = str(params.get("text") or params.get("item") or "").strip()
            if not text:
                return "todo: provide 'text'."
            try:
                priority = int(params.get("priority") or 0)
            except (TypeError, ValueError):
                priority = 0
            due = params.get("due") or None
            cur = _DB.execute(
                "INSERT INTO todos(list, text, due, priority, created_at) "
                "VALUES(?, ?, ?, ?, ?)",
                (lst, text, str(due) if due else None,
                 max(0, min(3, priority)), _now()),
            )
            _DB.commit()
            return f"Added #{cur.lastrowid} to {lst}: {text}"

        if action == "list":
            status = str(params.get("status") or "open").lower().strip()
            if status == "all":
                rows = _DB.execute(
                    "SELECT * FROM todos WHERE list=? "
                    "ORDER BY done, priority DESC, id", (lst,)
                ).fetchall()
            elif status == "done":
                rows = _DB.execute(
                    "SELECT * FROM todos WHERE list=? AND done=1 "
                    "ORDER BY done_at DESC", (lst,)
                ).fetchall()
            else:
                rows = _DB.execute(
                    "SELECT * FROM todos WHERE list=? AND done=0 "
                    "ORDER BY priority DESC, id", (lst,)
                ).fetchall()
            if not rows:
                return f"{lst}: empty."
            lines = [f"{lst} ({len(rows)} item(s)):"]
            for r in rows:
                lines.append("  " + _fmt(r))
            return "\n".join(lines)

        if action == "done":
            try:
                tid = int(params.get("id"))
            except (TypeError, ValueError):
                return "todo done: provide numeric 'id'."
            res = _DB.execute(
                "UPDATE todos SET done=1, done_at=? WHERE id=?",
                (_now(), tid),
            )
            _DB.commit()
            if res.rowcount == 0:
                return f"todo #{tid}: not found."
            return f"Marked #{tid} done."

        if action == "undo":
            try:
                tid = int(params.get("id"))
            except (TypeError, ValueError):
                return "todo undo: provide numeric 'id'."
            res = _DB.execute(
                "UPDATE todos SET done=0, done_at=NULL WHERE id=?", (tid,),
            )
            _DB.commit()
            if res.rowcount == 0:
                return f"todo #{tid}: not found."
            return f"Reopened #{tid}."

        if action == "delete":
            try:
                tid = int(params.get("id"))
            except (TypeError, ValueError):
                return "todo delete: provide numeric 'id'."
            res = _DB.execute("DELETE FROM todos WHERE id=?", (tid,))
            _DB.commit()
            if res.rowcount == 0:
                return f"todo #{tid}: not found."
            return f"Deleted #{tid}."

        if action == "search":
            q = str(params.get("query") or "").strip()
            if not q:
                return "todo search: provide 'query'."
            rows = _DB.execute(
                "SELECT * FROM todos WHERE text LIKE ? "
                "ORDER BY done, priority DESC, id",
                (f"%{q}%",),
            ).fetchall()
            if not rows:
                return f"No matches for {q!r}."
            return "\n".join([f"Search {q!r}:"] + [f"  {_fmt(r)}" for r in rows])

        if action == "clear":
            res = _DB.execute("DELETE FROM todos WHERE list=?", (lst,))
            _DB.commit()
            return f"Cleared {res.rowcount} item(s) from {lst}."

        if action == "lists":
            rows = _DB.execute(
                "SELECT list, SUM(1-done) AS open, SUM(done) AS done, COUNT(*) AS total "
                "FROM todos GROUP BY list ORDER BY list"
            ).fetchall()
            if not rows:
                return "No lists yet."
            return "\n".join(
                [f"  {r['list']}: {r['open']} open / {r['done']} done / {r['total']} total"
                 for r in rows]
            )

        return f"todo: unknown action {action!r}."


TOOL = {
    "name": "todo",
    "description": (
        "Local todo / shopping / notes manager. Use when the user asks you "
        "to remember something they need to DO rather than something they "
        "ARE (contrast with save_memory, which stores facts about them). "
        "Lists are free-form tags: 'todo' (default), 'shopping', 'errands', "
        "'work', 'ideas', etc. 'add' creates an item; 'list' shows open "
        "items by default (status=all for the whole list, status=done for "
        "history); 'done' / 'undo' / 'delete' manage a single id; 'search' "
        "finds across every list; 'clear' empties a named list; 'lists' "
        "shows the current lists with counts. Items survive restarts. "
        "Never tell the user 'I'll remember' and then skip calling this — "
        "if you don't call the tool, the item is lost."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "add | list | done | undo | delete | search | clear | lists",
            },
            "text":     {"type": "STRING",  "description": "Item text (for 'add')."},
            "list":     {"type": "STRING",  "description": "List name (free-form tag, e.g. shopping, errands)."},
            "due":      {"type": "STRING",  "description": "Due date YYYY-MM-DD (optional)."},
            "priority": {"type": "INTEGER", "description": "0 normal, 1 !, 2 !!, 3 !!! (default 0)."},
            "status":   {"type": "STRING",  "description": "For 'list': open (default) | done | all."},
            "id":       {"type": "INTEGER", "description": "Item id (for done/undo/delete)."},
            "query":    {"type": "STRING",  "description": "Substring for 'search'."},
        },
        "required": ["action"],
    },
    "handler": todo,
}
