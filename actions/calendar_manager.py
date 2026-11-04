"""
Calendar manager — wraps macOS Calendar.app via AppleScript.

Handles: create event, list upcoming events. Falls back cleanly with a
clear error when the user hasn't granted Automation permission (-1743).
"""
from __future__ import annotations

import platform
from typing import Any


def calendar_manager(
    parameters:    dict | None = None,
    response: Any = None,
    player:   Any = None,
    session_memory: Any = None,
) -> str:
    """Manage events in the user's macOS Calendar.

    Actions:
      list   days=<n>  calendar=<name>              — upcoming events
      add    title=<x> start=<YYYY-MM-DD HH:MM>
             end=<...>|duration_min=<int>
             calendar=<name> location=<...> notes=<...>
    """
    params = parameters or {}
    action = str(params.get("action", "list")).lower().strip()

    if platform.system() != "Darwin":
        return "calendar_manager: only macOS is supported in this build."

    from actions import _mac_apps as _ap

    try:
        if action == "add":
            title = str(params.get("title") or "").strip()
            start = str(params.get("start") or "").strip()
            if not title or not start:
                return "calendar add: provide 'title' and 'start'."
            end = params.get("end")
            try:
                dur = int(params.get("duration_min") or 60)
            except (TypeError, ValueError):
                dur = 60
            return _ap.calendar_create_event(
                title=title,
                start=start,
                end=str(end) if end else None,
                duration_min=dur,
                calendar_name=params.get("calendar"),
                location=str(params.get("location") or ""),
                notes=str(params.get("notes") or ""),
            )

        if action == "list":
            try:
                days = int(params.get("days") or 7)
            except (TypeError, ValueError):
                days = 7
            evs = _ap.calendar_list_events(
                days=days, calendar_name=params.get("calendar"))
            if not evs:
                return f"No events in the next {days} day(s)."
            lines = [f"Events in the next {days} day(s):"]
            for e in evs[:40]:
                loc = f" @ {e['location']}" if e.get("location") else ""
                lines.append(f"  [{e['calendar']}] {e['start']} — {e['title']}{loc}")
            return "\n".join(lines)

        return f"calendar_manager: unknown action {action!r}."

    except _ap.AppleScriptError as e:
        err = str(e)
        if "-1743" in err:
            return ("Calendar access blocked. Open System Settings → "
                    "Privacy & Security → Automation and allow your terminal/"
                    "Python app to control Calendar.")
        return f"calendar_manager error: {err}"


TOOL = {
    "name": "calendar_manager",
    "description": (
        "Read the user's upcoming calendar events and create new ones in "
        "the macOS Calendar app. Use for 'what's on my calendar this week', "
        "'schedule a meeting with X at 3pm tomorrow', 'book a dentist "
        "appointment on Friday'. 'list' with days= shows upcoming events "
        "(default 7); 'add' creates one. ALWAYS resolve relative dates "
        "(today, tomorrow, next Monday) to absolute YYYY-MM-DD HH:MM in "
        "the local timezone before calling. First call opens a macOS "
        "Automation permission dialog; the user grants once. Not for "
        "quick personal reminders — use `reminder` or `todo` instead."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action":       {"type": "STRING",  "description": "add | list"},
            "title":        {"type": "STRING",  "description": "Event title (for 'add')."},
            "start":        {"type": "STRING",  "description": "YYYY-MM-DD HH:MM in local time."},
            "end":          {"type": "STRING",  "description": "YYYY-MM-DD HH:MM — omit to use duration_min."},
            "duration_min": {"type": "INTEGER", "description": "Duration in minutes (default 60)."},
            "calendar":     {"type": "STRING",  "description": "Target calendar name (omit for default)."},
            "location":     {"type": "STRING",  "description": "Event location (optional)."},
            "notes":        {"type": "STRING",  "description": "Event notes/body (optional)."},
            "days":         {"type": "INTEGER", "description": "For 'list': days ahead (default 7)."},
        },
        "required": ["action"],
    },
    "handler": calendar_manager,
}
