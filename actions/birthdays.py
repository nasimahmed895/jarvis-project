"""
Birthday / anniversary reminders.

Reads ``birthday`` values stored on CRM contacts and surfaces the ones
coming up. Anniversaries that aren't someone's personal birthday are
carried via the existing CRM follow-up queue — a yearly follow-up with
a date is semantically identical to an anniversary reminder, so we do
not grow a third store.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from core import crm as _crm


_CONNECTED = False


def _ensure() -> None:
    global _CONNECTED
    if _CONNECTED:
        return
    db = Path(__file__).resolve().parent.parent / "memory" / "crm.sqlite"
    _crm.connect(db)
    _CONNECTED = True


_DATE_RE = re.compile(
    r"^(?P<y>\d{4})?[-/]?(?P<m>\d{1,2})[-/](?P<d>\d{1,2})$"
)


def _parse_birthday(raw: str | None) -> tuple[int, int] | None:
    """Accept YYYY-MM-DD, MM-DD, YYYY/MM/DD, MM/DD. Return (month, day) or
    None. Year is optional because birthday reminders don't care about it —
    only the upcoming month/day matters."""
    if not raw:
        return None
    s = str(raw).strip()
    m = _DATE_RE.match(s)
    if not m:
        return None
    try:
        month = int(m.group("m"))
        day = int(m.group("d"))
    except (TypeError, ValueError):
        return None
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    return (month, day)


def _days_until(month: int, day: int,
                today: date | None = None) -> int:
    today = today or date.today()
    y = today.year
    try:
        target = date(y, month, day)
    except ValueError:
        # Feb 29 in non-leap year: fall back to Feb 28
        if (month, day) == (2, 29):
            target = date(y, 2, 28)
        else:
            return 9999
    if target < today:
        try:
            target = date(y + 1, month, day)
        except ValueError:
            target = date(y + 1, 2, 28)
    return (target - today).days


def _compute_age(birthday_raw: str | None) -> int | None:
    """If the stored birthday has a year, compute the age the person turns
    on their next birthday. Return None if year wasn't recorded."""
    if not birthday_raw:
        return None
    s = str(birthday_raw).strip()
    m = _DATE_RE.match(s)
    if not m or not m.group("y"):
        return None
    try:
        y = int(m.group("y"))
    except (TypeError, ValueError):
        return None
    today = date.today()
    # Age on their next birthday.
    try:
        mm, dd = int(m.group("m")), int(m.group("d"))
        next_bd = date(today.year, mm, dd)
        if next_bd < today:
            next_bd = date(today.year + 1, mm, dd)
        return next_bd.year - y
    except Exception:
        return None


def _fmt(contact: dict, days: int, age: int | None) -> str:
    tier = contact.get("tier", "regular")
    name = contact.get("name", "?")
    bd = contact.get("birthday") or "?"
    bits = [f"[{tier}] {name}"]
    if age is not None:
        bits.append(f"turning {age}")
    bits.append(f"in {days} day(s)")
    bits.append(f"({bd})")
    return "  - " + " ".join(bits)


def upcoming_birthdays(days: int = 30,
                        today: date | None = None) -> list[dict]:
    """Return every contact whose birthday falls within ``days`` from
    ``today``. Each entry is augmented with ``_days_until`` and
    ``_age`` keys so callers don't have to recompute."""
    _ensure()
    today = today or date.today()
    out: list[dict] = []
    # Pull a wide chunk; CRM lists only return 100 by default.
    for c in _crm.list_contacts(limit=5000):
        bday = _parse_birthday(c.get("birthday"))
        if not bday:
            continue
        d_until = _days_until(*bday, today=today)
        if d_until > days:
            continue
        enriched = dict(c)
        enriched["_days_until"] = d_until
        enriched["_age"] = _compute_age(c.get("birthday"))
        out.append(enriched)
    out.sort(key=lambda r: r["_days_until"])
    return out


def birthdays(
    parameters:    dict | None = None,
    response: Any = None,
    player:   Any = None,
    session_memory: Any = None,
) -> str:
    """Report upcoming birthdays from CRM contacts.

    Actions:
      upcoming   days= (default 30) — contacts with a birthday within N days
      today      — just today's birthdays
    """
    _ensure()
    p = parameters or {}
    action = str(p.get("action", "upcoming")).lower().strip()

    if action in ("today", "upcoming"):
        try:
            days = 0 if action == "today" else int(p.get("days", 30))
        except (TypeError, ValueError):
            days = 30
        rows = upcoming_birthdays(days=days)
        if not rows:
            return (f"No birthdays within {days} day(s)."
                    if days else "No birthdays today.")
        header = ("Today's birthdays:" if action == "today"
                   else f"Birthdays within {days} day(s) ({len(rows)}):")
        lines = [header]
        for r in rows:
            lines.append(_fmt(r, r["_days_until"], r.get("_age")))
        return "\n".join(lines)

    return (f"birthdays: unknown action {action!r}. "
            "Actions: upcoming | today")


TOOL = {
    "name": "birthdays",
    "description": (
        "Surface upcoming birthdays from the CRM. Store a 'birthday' on "
        "a contact first (via the crm action; accepts YYYY-MM-DD or "
        "MM-DD). This action returns contacts whose birthday falls "
        "within the given window, tiered so VIPs/family surface first. "
        "Anniversaries that aren't a personal birthday should ride on "
        "the CRM follow-up queue with a yearly cadence; that's the same "
        "shape of data and the gatekeeper already surfaces it."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "upcoming | today (default upcoming)"},
            "days":   {"type": "INTEGER",
                       "description": "Horizon in days (default 30)."},
        },
        "required": [],
    },
    "handler": birthdays,
}
