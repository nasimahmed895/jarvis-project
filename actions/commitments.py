"""
Commitments action — track, surface, and resolve things the user has
promised to do.

Wraps ``core/commitments.py``. Expected calling patterns:

  * The model (or the planner) calls ``add`` directly whenever the user
    makes an explicit promise.
  * ``extract`` scans a chunk of recent transcript and stores every
    first-person commitment it finds. Call this on every utterance for
    hands-free capture.
  * ``due`` is what the proactive engine and morning briefing use to
    surface commitments ("you promised to send the proposal by Friday").
  * ``resolve`` / ``snooze`` / ``cancel`` close the loop.
"""
from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import Any

from core import commitments as _c


_CONNECTED = False


def _ensure() -> None:
    global _CONNECTED
    if _CONNECTED:
        return
    db = Path(__file__).resolve().parent.parent / "memory" / "commitments.sqlite"
    _c.connect(db)
    _CONNECTED = True


def _fmt_due(ts: float | None) -> str:
    if not ts:
        return "no date"
    return _dt.datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")


def _fmt(row: dict) -> str:
    return (f"#{row['id']} [{row['status']}] {row['text']} "
            f"(due {_fmt_due(row.get('due_ts'))})")


def _parse_due(raw: Any) -> float | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw).strip()
    try:
        return float(s)
    except ValueError:
        pass
    # Delegate to the natural-language resolver inside core/commitments.py.
    dt = _c._resolve_relative(s)
    if dt is not None:
        return dt.timestamp()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return _dt.datetime.strptime(s, fmt).timestamp()
        except ValueError:
            continue
    return None


def commitments(
    parameters:    dict | None = None,
    response: Any = None,
    player:   Any = None,
    session_memory: Any = None,
) -> str:
    """Track and surface commitments the user has made.

    Actions:
      add       text= due= contact_id= source=
      extract   text=   — mine a chunk of transcript for commitments
      list      status=open|done|missed|cancelled|all
      due       hours=  — commitments due within N hours (default 24)
      resolve   id=
      miss      id=
      snooze    id= hours=
      cancel    id=
      sweep_missed hours=  — mark stale open ones 'missed' (default 24)
    """
    _ensure()
    p = parameters or {}
    action = str(p.get("action", "")).lower().strip()

    if action == "add":
        text = str(p.get("text") or "").strip()
        if not text:
            return "commitments add: provide 'text'."
        due_ts = _parse_due(p.get("due"))
        cid = p.get("contact_id")
        try:
            cid = int(cid) if cid is not None else None
        except (TypeError, ValueError):
            cid = None
        source = str(p.get("source") or "manual").lower()
        try:
            new_id = _c.add_commitment(text, due_ts=due_ts,
                                        contact_id=cid, source=source)
        except ValueError as e:
            return f"commitments add: {e}"
        return f"Logged commitment #{new_id}: {text} (due {_fmt_due(due_ts)})."

    if action == "extract":
        text = str(p.get("text") or "").strip()
        if not text:
            return "commitments extract: provide 'text'."
        found = _c.extract_from_text(text)
        if not found:
            return "No commitments detected."
        ids = _c.extract_and_store(text, source="extracted")
        lines = [f"Extracted {len(found)} commitment(s):"]
        for e, cid in zip(found, ids):
            lines.append(f"  #{cid} {e.text} (due {_fmt_due(e.due_ts)})")
        return "\n".join(lines)

    if action == "list":
        status = str(p.get("status") or "open").lower()
        if status == "all":
            rows = _c.list_all()
        elif status == "open":
            rows = _c.list_open()
        else:
            rows = _c.list_all(status=status)
        if not rows:
            return f"No commitments with status={status}."
        return (f"Commitments ({status}, {len(rows)}):\n"
                + "\n".join("  " + _fmt(r) for r in rows))

    if action == "due":
        try:
            hours = float(p.get("hours", 24))
        except (TypeError, ValueError):
            hours = 24.0
        rows = _c.due_within(hours)
        if not rows:
            return f"No commitments due within {hours:g}h."
        return (f"Due within {hours:g}h ({len(rows)}):\n"
                + "\n".join("  " + _fmt(r) for r in rows))

    if action == "resolve":
        try:
            cid = int(p.get("id"))
        except (TypeError, ValueError):
            return "commitments resolve: provide numeric 'id'."
        return (f"Resolved commitment #{cid}."
                if _c.resolve(cid)
                else f"Commitment #{cid} not found or not open.")

    if action == "miss":
        try:
            cid = int(p.get("id"))
        except (TypeError, ValueError):
            return "commitments miss: provide numeric 'id'."
        return (f"Marked commitment #{cid} missed."
                if _c.miss(cid)
                else f"Commitment #{cid} not found or not open.")

    if action == "snooze":
        try:
            cid = int(p.get("id"))
        except (TypeError, ValueError):
            return "commitments snooze: provide numeric 'id'."
        try:
            hours = float(p.get("hours", 24))
        except (TypeError, ValueError):
            hours = 24.0
        return (f"Snoozed commitment #{cid} by {hours:g}h."
                if _c.snooze(cid, hours)
                else f"Commitment #{cid} not found.")

    if action == "cancel":
        try:
            cid = int(p.get("id"))
        except (TypeError, ValueError):
            return "commitments cancel: provide numeric 'id'."
        return (f"Cancelled commitment #{cid}."
                if _c.cancel(cid)
                else f"Commitment #{cid} not found.")

    if action == "sweep_missed":
        try:
            hours = float(p.get("hours", 24))
        except (TypeError, ValueError):
            hours = 24.0
        n = _c.sweep_missed(grace_hours=hours)
        return f"Marked {n} overdue commitment(s) missed."

    return (f"commitments: unknown action {action!r}. "
            "Actions: add | extract | list | due | resolve | miss | "
            "snooze | cancel | sweep_missed")


TOOL = {
    "name": "commitments",
    "description": (
        "Track promises the user makes. Store with 'add' when the user "
        "explicitly says they will do something; call 'extract' on raw "
        "transcript to auto-detect first-person commitments ('I'll send "
        "it by Friday'). Use 'due' every morning / proactive tick to "
        "surface what's owed. Use 'resolve' when they confirm completion, "
        "'snooze' when they ask for more time, 'miss' when they admit "
        "slipping. The extractor is pattern-based and offline — fast and "
        "safe to call on every utterance."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": (
                "add | extract | list | due | resolve | miss | snooze | "
                "cancel | sweep_missed")},
            "id":      {"type": "INTEGER", "description": "Commitment row id."},
            "text":    {"type": "STRING",  "description": "Commitment text (add) or raw transcript to mine (extract)."},
            "due":     {"type": "STRING",  "description": "Due date (YYYY-MM-DD or YYYY-MM-DD HH:MM or 'tomorrow' / 'friday' / 'next week' / 'end of week')."},
            "hours":   {"type": "NUMBER",  "description": "Window in hours for 'due' / 'snooze' / 'sweep_missed'."},
            "status":  {"type": "STRING",  "description": "list filter: open | done | missed | cancelled | all"},
            "contact_id": {"type": "INTEGER", "description": "Link to a CRM contact."},
            "source":  {"type": "STRING",  "description": "manual | extracted | plan (default manual)."},
        },
        "required": ["action"],
    },
    "handler": commitments,
}
