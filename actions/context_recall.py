"""
Context recall — query the short-lived multimodal context buffer.

Every vision analysis, OCR pass, and emotion read can push a summary
into the buffer. This action gives the model a way to look back at
them in the same conversation without re-running the tool.
"""
from __future__ import annotations

import datetime as _dt
from typing import Any

from core import context_buffer as _cb


def _fmt(entries: list[dict]) -> str:
    if not entries:
        return "(context buffer is empty for this filter)"
    lines = []
    for e in entries:
        when = _dt.datetime.fromtimestamp(float(e.get("ts", 0.0))) \
            .strftime("%H:%M:%S")
        lines.append(f"  [{when}] {e.get('kind','?')}: "
                     f"{e.get('summary','')}")
    return "\n".join(lines)


def context_recall(
    parameters:    dict | None = None,
    response: Any = None,
    player:   Any = None,
    session_memory: Any = None,
) -> str:
    """Query the recent multimodal context buffer.

    Actions:
      recent   limit= kinds=<csv>  since_seconds=
      search   query=
      size
      clear
      record   kind= summary=   (planner can log a free-form observation)
    """
    p = parameters or {}
    action = str(p.get("action", "recent")).lower().strip()

    if action == "recent":
        try:
            limit = int(p.get("limit", 10))
        except (TypeError, ValueError):
            limit = 10
        kinds = p.get("kinds")
        if isinstance(kinds, str):
            kinds = [k.strip() for k in kinds.split(",") if k.strip()]
        try:
            since = float(p.get("since_seconds")) if p.get("since_seconds") else None
        except (TypeError, ValueError):
            since = None
        return _fmt(_cb.recent(limit=limit, kinds=kinds,
                                since_seconds=since))

    if action == "search":
        q = str(p.get("query") or "").strip()
        if not q:
            return "context_recall search: provide 'query'."
        return _fmt(_cb.search(q, limit=int(p.get("limit", 10) or 10)))

    if action == "size":
        return f"Context buffer holds {_cb.size()} entries."

    if action == "clear":
        _cb.clear()
        return "Context buffer cleared."

    if action == "record":
        kind = str(p.get("kind") or "note").strip() or "note"
        summary = str(p.get("summary") or "").strip()
        if not summary:
            return "context_recall record: provide 'summary'."
        _cb.record(kind, summary)
        return f"Recorded ({kind})."

    return (f"context_recall: unknown action {action!r}. "
            "Actions: recent | search | size | clear | record")


TOOL = {
    "name": "context_recall",
    "description": (
        "Query the short-lived multimodal context buffer. Every "
        "vision_analyze call, screenshot, OCR pass, emotion read, and "
        "explicit 'record' pushes a summary line into this ring buffer "
        "(max 200 entries, newest first). Use 'recent' to see the last "
        "few observations, 'search' to find one by keyword, 'record' "
        "to log a free-form note the voice loop can later refer to. "
        "The buffer is in-memory per session — this is not long-term "
        "memory; use save_memory for durable facts."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action":        {"type": "STRING",
                              "description": "recent | search | size | clear | record"},
            "limit":         {"type": "INTEGER", "description": "Row cap."},
            "kinds":         {"type": "STRING",
                              "description": "Comma-separated kind filters (e.g. 'vision,emotion')."},
            "since_seconds": {"type": "NUMBER",
                              "description": "Only entries newer than this many seconds."},
            "query":         {"type": "STRING",
                              "description": "Substring for 'search'."},
            "kind":          {"type": "STRING",
                              "description": "Kind tag for 'record'."},
            "summary":       {"type": "STRING",
                              "description": "Line to log for 'record'."},
        },
        "required": ["action"],
    },
    "handler": context_recall,
}
