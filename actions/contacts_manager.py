"""Contacts manager — macOS Contacts.app lookup via AppleScript."""
from __future__ import annotations

import platform
from typing import Any


def contacts_manager(
    parameters:    dict | None = None,
    response: Any = None,
    player:   Any = None,
    session_memory: Any = None,
) -> str:
    """Search / list macOS contacts.

    Actions:
      search  query=<name substring>  limit=<n>  — return matching people
    """
    params = parameters or {}
    action = str(params.get("action", "search")).lower().strip()
    if platform.system() != "Darwin":
        return "contacts_manager: only macOS is supported in this build."
    from actions import _mac_apps as _ap

    try:
        if action == "search":
            q = str(params.get("query") or "").strip()
            if not q:
                return "contacts search: provide 'query'."
            try:
                limit = int(params.get("limit") or 10)
            except (TypeError, ValueError):
                limit = 10
            hits = _ap.contacts_search(q, limit=max(1, min(50, limit)))
            if not hits:
                return f"No contacts match {q!r}."
            lines = [f"Contacts matching {q!r}:"]
            for h in hits:
                e = ", ".join(h["emails"]) if h["emails"] else "—"
                p = ", ".join(h["phones"]) if h["phones"] else "—"
                lines.append(f"  {h['name']}  ✉ {e}  ☏ {p}")
            return "\n".join(lines)
        return f"contacts_manager: unknown action {action!r}."
    except _ap.AppleScriptError as e:
        err = str(e)
        if "-1743" in err:
            return ("Contacts access blocked. Open System Settings → "
                    "Privacy & Security → Automation and allow your "
                    "terminal/Python app to control Contacts.")
        return f"contacts_manager error: {err}"


TOOL = {
    "name": "contacts_manager",
    "description": (
        "Search the user's macOS Contacts app by name. Returns matching "
        "people with their emails and phone numbers. Use when the user "
        "asks 'what is <name>'s email', 'call <name>', or anywhere you "
        "need a contact detail before another action (send_message, "
        "email_manager compose). Reports empty politely when there's no "
        "match — do NOT invent an address."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",  "description": "search"},
            "query":  {"type": "STRING",  "description": "Name or substring to look up."},
            "limit":  {"type": "INTEGER", "description": "Max results (default 10, max 50)."},
        },
        "required": ["action"],
    },
    "handler": contacts_manager,
}
