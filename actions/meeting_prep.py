"""
Meeting preparation — pre-meeting briefing packet.

Aggregates everything JARVIS already knows about an upcoming meeting into
one readable brief: event details, who's attending (CRM lookup, last-seen,
recent interactions), open follow-ups with those attendees, open
commitments related to them, and detected calendar conflicts.

This module is pure aggregation — it does not call the LLM. All the inputs
come from stores the user has already populated (calendar, CRM,
commitments, follow-ups). Keeping the brief deterministic means it can be
regenerated minutes before the meeting without risking a model timeout.
"""
from __future__ import annotations

import platform
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from core import crm as _crm
from core import commitments as _c
from core import gatekeeper as _gk


_CRM_CONNECTED = False
_COM_CONNECTED = False


def _ensure() -> None:
    global _CRM_CONNECTED, _COM_CONNECTED
    base = Path(__file__).resolve().parent.parent / "memory"
    if not _CRM_CONNECTED:
        try:
            _crm.connect(base / "crm.sqlite")
            _CRM_CONNECTED = True
        except Exception as e:
            print(f"[MeetingPrep] CRM unavailable: {e}")
    if not _COM_CONNECTED:
        try:
            _c.connect(base / "commitments.sqlite")
            _COM_CONNECTED = True
        except Exception as e:
            print(f"[MeetingPrep] commitments unavailable: {e}")


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _parse_attendees(raw: Any) -> list[dict]:
    """Normalise a freeform attendee list into [{name, email}, ...].

    Accepts:
      - list[str]  — each item is either an email or a bare name
      - list[dict] — pass-through
      - str        — comma / newline / semicolon separated
    """
    out: list[dict] = []

    def add(name: str = "", email: str = ""):
        name = (name or "").strip()
        email = (email or "").strip().lower()
        if not (name or email):
            return
        out.append({"name": name, "email": email})

    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                add(item.get("name", ""), item.get("email", ""))
            elif isinstance(item, str):
                m = _EMAIL_RE.search(item)
                if m:
                    add(item.replace(m.group(0), "").strip(" <>,"),
                        m.group(0))
                else:
                    add(item)
    elif isinstance(raw, str):
        for piece in re.split(r"[,;\n]+", raw):
            piece = piece.strip()
            if not piece:
                continue
            m = _EMAIL_RE.search(piece)
            if m:
                add(piece.replace(m.group(0), "").strip(" <>,"),
                    m.group(0))
            else:
                add(piece)
    return out


def _lookup(attendee: dict) -> dict | None:
    email = attendee.get("email") or ""
    name = attendee.get("name") or ""
    if email:
        hit = _crm.find_by_email(email)
        if hit:
            return hit
    if name:
        rows = _crm.search_contacts(name, limit=1)
        if rows:
            return rows[0]
    return None


def _fmt_contact_line(attendee: dict, contact: dict | None) -> str:
    label = attendee.get("name") or attendee.get("email") or "(unknown)"
    if not contact:
        return f"  - {label}  _(not in CRM)_"
    tier = contact.get("tier", "regular")
    bits = [f"[{tier}]", contact.get("name", label)]
    if contact.get("role"):
        bits.append(f"· {contact['role']}")
    if contact.get("org"):
        bits.append(f"@ {contact['org']}")
    last = contact.get("last_seen")
    if last:
        days = (datetime.now().timestamp() - float(last)) / 86400.0
        bits.append(f"· last seen {days:.1f}d ago")
    return "  - " + " ".join(bits)


def _recent_interactions_block(contact: dict, limit: int = 3) -> list[str]:
    rows = _crm.recent_interactions(contact["id"], limit=limit)
    out: list[str] = []
    for r in rows:
        when = datetime.fromtimestamp(float(r["ts"])).strftime("%Y-%m-%d")
        subj = r.get("subject") or ""
        summ = r.get("summary") or ""
        out.append(f"      · {when} [{r['kind']}] {subj} {summ}".rstrip())
    return out


def _collect_events() -> list[dict]:
    """Pull upcoming calendar events. macOS only in this build — degrades
    to an empty list elsewhere."""
    if platform.system() != "Darwin":
        return []
    try:
        from actions import _mac_apps as _ap
        return _ap.calendar_list_events(days=7) or []
    except Exception as e:
        print(f"[MeetingPrep] calendar read failed: {e}")
        return []


def _find_next_event(events: Iterable[dict],
                     *, now: datetime | None = None) -> dict | None:
    now = now or datetime.now()
    for ev in events:
        start = ev.get("start")
        if not start:
            continue
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M",
                    "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(start, fmt)
            except ValueError:
                continue
            if dt >= now:
                return ev
            break
    return None


def _related_commitments(contacts: list[dict]) -> list[dict]:
    """Open commitments whose text references any attendee name or that
    are explicitly linked to an attendee contact."""
    if not contacts:
        return []
    rows = _c.list_open(limit=200)
    hits: list[dict] = []
    names_lower = [c["name"].lower() for c in contacts if c.get("name")]
    cids = {c["id"] for c in contacts}
    for r in rows:
        t = (r.get("text") or "").lower()
        if r.get("contact_id") in cids:
            hits.append(r)
            continue
        if any(n and n in t for n in names_lower):
            hits.append(r)
    return hits


def _related_follow_ups(contacts: list[dict]) -> list[dict]:
    cids = {c["id"] for c in contacts}
    if not cids:
        return []
    return [f for f in _crm.list_follow_ups(status="open", limit=200)
            if f.get("contact_id") in cids]


def _build_brief(event: dict,
                 extra_attendees: list[dict] | None = None) -> str:
    _ensure()
    title = event.get("title", "(untitled)")
    start = event.get("start", "?")
    end = event.get("end", "")
    location = event.get("location", "")
    calendar = event.get("calendar", "")
    notes = event.get("notes") or ""

    attendees = list(extra_attendees or [])
    for src in ("attendees", "invitees", "participants"):
        val = event.get(src)
        if val:
            attendees.extend(_parse_attendees(val))

    # De-dupe by (name, email) after lowercase.
    seen: set[tuple[str, str]] = set()
    unique_att: list[dict] = []
    for a in attendees:
        key = (a.get("name", "").lower(), a.get("email", "").lower())
        if key not in seen and (key[0] or key[1]):
            seen.add(key)
            unique_att.append(a)

    hits = [(a, _lookup(a)) for a in unique_att]
    known_contacts = [c for _, c in hits if c]

    lines = [f"# Meeting brief — {title}",
             f"When: {start}" + (f" – {end}" if end else ""),
             f"Where: {location}" if location else "Where: —",
             f"Calendar: {calendar}" if calendar else ""]
    lines = [l for l in lines if l]

    if notes.strip():
        lines.append("")
        lines.append(f"> {notes.strip()}")

    if unique_att:
        lines.append("")
        lines.append("## Attendees")
        for att, contact in hits:
            lines.append(_fmt_contact_line(att, contact))
            if contact:
                lines.extend(_recent_interactions_block(contact))
    else:
        lines.append("")
        lines.append("## Attendees")
        lines.append("  - (not listed on the event)")

    # Follow-ups owed to any of the known attendees.
    fus = _related_follow_ups(known_contacts)
    if fus:
        lines.append("")
        lines.append("## Open follow-ups with attendees")
        for f in fus:
            due = ""
            if f.get("due_ts"):
                due = " — due " + datetime.fromtimestamp(
                    float(f["due_ts"])).strftime("%Y-%m-%d")
            lines.append(f"  - {f.get('contact_name','?')}: "
                         f"{f['text']}{due}")

    # Commitments the user has made that relate to the attendees.
    coms = _related_commitments(known_contacts)
    if coms:
        lines.append("")
        lines.append("## Your commitments mentioning these people")
        for c in coms:
            due = ""
            if c.get("due_ts"):
                due = " — due " + datetime.fromtimestamp(
                    float(c["due_ts"])).strftime("%Y-%m-%d")
            lines.append(f"  - {c['text']}{due}")

    # Calendar conflicts around the event's start.
    if start:
        others = [e for e in _collect_events()
                  if (e.get("start"), e.get("title"))
                  != (event.get("start"), event.get("title"))]
        conflicts = _gk.detect_conflict(others, start, end or None)
        if conflicts:
            lines.append("")
            lines.append("## Calendar conflicts")
            for c in conflicts[:5]:
                loc = f" @ {c.get('location')}" if c.get("location") else ""
                lines.append(f"  - {c.get('start','?')} — "
                             f"{c.get('title','(untitled)')}{loc}")

    return "\n".join(lines)


def meeting_prep(
    parameters:    dict | None = None,
    response: Any = None,
    player:   Any = None,
    session_memory: Any = None,
) -> str:
    """Build a pre-meeting briefing packet.

    Actions:
      next        — brief for the next upcoming event on the calendar
      brief       — brief for an explicit event: title= start= [end=]
                    [location=] [attendees=]  (can also pass notes=)
      upcoming    — list the next few events, lightest form
                    (days=<n> default 3)
    """
    _ensure()
    p = parameters or {}
    action = str(p.get("action", "next")).lower().strip()

    if action == "upcoming":
        try:
            days = int(p.get("days") or 3)
        except (TypeError, ValueError):
            days = 3
        events = _collect_events()[:max(1, min(20, days * 4))]
        if not events:
            return "No upcoming events found (or calendar inaccessible)."
        lines = [f"Next {len(events)} event(s):"]
        for ev in events:
            loc = f" @ {ev.get('location')}" if ev.get("location") else ""
            lines.append(f"  {ev.get('start','?')} — "
                         f"{ev.get('title','(untitled)')}{loc}")
        return "\n".join(lines)

    if action == "next":
        event = _find_next_event(_collect_events())
        if not event:
            return "No upcoming event to brief on."
        return _build_brief(event,
                             extra_attendees=_parse_attendees(
                                 p.get("attendees")))

    if action == "brief":
        title = str(p.get("title") or "").strip()
        start = str(p.get("start") or "").strip()
        if not title or not start:
            return "meeting_prep brief: provide 'title' and 'start'."
        event = {
            "title":    title,
            "start":    start,
            "end":      str(p.get("end") or ""),
            "location": str(p.get("location") or ""),
            "calendar": str(p.get("calendar") or ""),
            "notes":    str(p.get("notes") or ""),
            "attendees": _parse_attendees(p.get("attendees")),
        }
        return _build_brief(event)

    return (f"meeting_prep: unknown action {action!r}. "
            "Actions: next | brief | upcoming")


TOOL = {
    "name": "meeting_prep",
    "description": (
        "Build a pre-meeting briefing packet. 'next' grabs the next "
        "event from the user's calendar and assembles context: who "
        "attends (CRM tier, last interaction, recent emails/meetings), "
        "any open follow-ups with them, commitments you've made that "
        "mention them, and any detected calendar conflicts. 'brief' "
        "does the same for an event you specify by title + start + "
        "attendees (useful before an event exists in the calendar). "
        "'upcoming' shows the next few events without the full brief. "
        "Deterministic — no LLM call — so safe to run minutes before "
        "the meeting starts."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action":   {"type": "STRING", "description": "next | brief | upcoming"},
            "title":    {"type": "STRING", "description": "Event title (brief)."},
            "start":    {"type": "STRING", "description": "YYYY-MM-DD HH:MM (brief)."},
            "end":      {"type": "STRING", "description": "Optional end time (brief)."},
            "location": {"type": "STRING", "description": "Location (brief)."},
            "calendar": {"type": "STRING", "description": "Calendar name (brief, optional)."},
            "notes":    {"type": "STRING", "description": "Notes already on the event (brief, optional)."},
            "attendees": {"type": "STRING", "description": "Comma / newline separated list of names and/or emails."},
            "days":     {"type": "INTEGER", "description": "Days ahead for 'upcoming' (default 3)."},
        },
        "required": ["action"],
    },
    "handler": meeting_prep,
}
