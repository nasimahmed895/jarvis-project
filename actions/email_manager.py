"""
Email manager — macOS Mail.app via AppleScript.

Reads inbox, summarises a message, drafts or sends an outgoing message.
Sending is gated by `_gate_sensitive` (or trust mode): a single confirmation
on the HUD before any real send. Compose-only (visible=true, no send) is
always safe.
"""
from __future__ import annotations

import platform
from typing import Any


def _trust() -> bool:
    try:
        from actions.browser_control import _trust_me
        return _trust_me()
    except Exception:
        return False


def email_manager(
    parameters:    dict | None = None,
    response: Any = None,
    player:   Any = None,
    session_memory: Any = None,
) -> str:
    """Manage Apple Mail.

    Actions:
      list      mailbox=INBOX  limit=10  unread_only=false   — recent messages
      read      id=<message-id>                               — full body
      compose   to=<addr> subject=<x> body=<y> cc=<z>          — open draft
      send      to=... subject=... body=... cc=...              — DRAFT & SEND (gated)
    """
    params = parameters or {}
    action = str(params.get("action", "list")).lower().strip()
    if platform.system() != "Darwin":
        return "email_manager: only macOS is supported in this build."
    from actions import _mac_apps as _ap

    try:
        if action == "list":
            mb = str(params.get("mailbox") or "INBOX").strip() or "INBOX"
            try:
                limit = int(params.get("limit") or 10)
            except (TypeError, ValueError):
                limit = 10
            unread = bool(params.get("unread_only") or False)
            msgs = _ap.mail_list_recent(
                mailbox=mb,
                limit=max(1, min(50, limit)),
                unread_only=unread,
            )
            if not msgs:
                return f"No {'unread ' if unread else ''}messages in {mb}."
            lines = [f"{mb} ({len(msgs)} message(s)):"]
            for m in msgs:
                mark = "●" if m.get("unread") else "○"
                lines.append(f"  {mark} [{m['id']}] {m['from']}  —  {m['subject'][:80]}  ({m['date']})")
            return "\n".join(lines)

        if action == "read":
            mid = str(params.get("id") or "").strip()
            if not mid:
                return "email read: provide 'id'."
            msg = _ap.mail_read(mid)
            body = (msg.get("body") or "").strip()
            if len(body) > 4000:
                body = body[:4000] + "\n…(truncated)"
            return (f"From: {msg.get('from')}\n"
                    f"Subject: {msg.get('subject')}\n---\n{body}")

        if action == "compose":
            to      = str(params.get("to") or "").strip()
            subject = str(params.get("subject") or "").strip()
            body    = str(params.get("body") or "").strip()
            cc      = str(params.get("cc") or "").strip()
            if not to or not subject:
                return "email compose: provide 'to' and 'subject'."
            return _ap.mail_compose(to=to, subject=subject, body=body,
                                     cc=cc, send=False)

        if action == "send":
            to      = str(params.get("to") or "").strip()
            subject = str(params.get("subject") or "").strip()
            body    = str(params.get("body") or "").strip()
            cc      = str(params.get("cc") or "").strip()
            if not to or not subject:
                return "email send: provide 'to' and 'subject'."

            def _do_send() -> str:
                return _ap.mail_compose(to=to, subject=subject, body=body,
                                         cc=cc, send=True)

            if _trust():
                return _do_send()
            try:
                from core import confirm as _confirm
            except Exception:
                _confirm = None  # type: ignore
            if _confirm is None or not _confirm.is_bound():
                return _do_send()
            return _confirm.request(
                key=f"mail-send-{to}-{subject[:20]}",
                title="Send email",
                detail=(f"To: {to}\nSubject: {subject}\n\n"
                        f"{body[:300]}" + ("…" if len(body) > 300 else "")),
                run=_do_send,
            )

        return f"email_manager: unknown action {action!r}."
    except _ap.AppleScriptError as e:
        err = str(e)
        if "-1743" in err:
            return ("Mail access blocked. Open System Settings → Privacy & "
                    "Security → Automation and allow your terminal/Python "
                    "app to control Mail.")
        return f"email_manager error: {err}"


TOOL = {
    "name": "email_manager",
    "description": (
        "Read, draft, and send email through the user's macOS Mail.app. "
        "'list' summarises recent messages (unread_only=true to filter); "
        "'read' pulls the full body of one id from that list; 'compose' "
        "opens a draft window for review; 'send' actually sends — this "
        "goes through the HUD confirmation gate unless trust_me is on. "
        "ALWAYS use 'list' first to get real ids before calling 'read'; "
        "never invent an id. For addresses, call contacts_manager to look "
        "up a name rather than guessing an address."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action":      {"type": "STRING",  "description": "list | read | compose | send"},
            "mailbox":     {"type": "STRING",  "description": "Mailbox name (default INBOX)."},
            "limit":       {"type": "INTEGER", "description": "Max messages for 'list' (default 10)."},
            "unread_only": {"type": "BOOLEAN", "description": "List unread messages only."},
            "id":          {"type": "STRING",  "description": "Message id (for 'read')."},
            "to":          {"type": "STRING",  "description": "Recipient address (compose/send)."},
            "subject":     {"type": "STRING",  "description": "Subject line."},
            "body":        {"type": "STRING",  "description": "Message body."},
            "cc":          {"type": "STRING",  "description": "CC address (optional)."},
        },
        "required": ["action"],
    },
    "handler": email_manager,
}
