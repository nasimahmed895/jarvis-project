"""
Travel action — trips and legs (flights, hotels, transfers, activities).

Wraps ``core/travel.py``. Local store only. Pairs with the existing
browser-based flight_finder for research, and with finance for expense
tracking (the planner can push a leg's cost into finance.add_expense
when the user confirms the booking).
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any

from core import travel as _t


_CONNECTED = False


def _ensure() -> None:
    global _CONNECTED
    if _CONNECTED:
        return
    db = Path(__file__).resolve().parent.parent / "memory" / "travel.sqlite"
    _t.connect(db)
    _CONNECTED = True


def _int(raw, default=None):
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _fmt_trip(t: dict) -> str:
    bits = [f"#{t['id']} [{t['status']}] {t['name']}"]
    if t.get("destination"):
        bits.append(f"→ {t['destination']}")
    if t.get("start_date"):
        rng = t["start_date"]
        if t.get("end_date"):
            rng += " → " + t["end_date"]
        bits.append(f"· {rng}")
    return " ".join(bits)


def _fmt_leg(l: dict) -> str:
    def when(ts):
        return _dt.datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M") if ts else "?"
    bits = [f"  #{l['id']} [{l['kind']}]"]
    if l.get("carrier"):
        bits.append(l["carrier"])
    if l.get("from_loc") or l.get("to_loc"):
        bits.append(f"{l.get('from_loc','?')} → {l.get('to_loc','?')}")
    if l.get("start_ts"):
        bits.append(f"start {when(l['start_ts'])}")
    if l.get("end_ts"):
        bits.append(f"end {when(l['end_ts'])}")
    if l.get("reference"):
        bits.append(f"ref={l['reference']}")
    if l.get("cost") is not None:
        bits.append(f"{l.get('currency') or 'USD'} {float(l['cost']):,.2f}")
    return " · ".join(bits)


def travel(
    parameters:    dict | None = None,
    response: Any = None,
    player:   Any = None,
    session_memory: Any = None,
) -> str:
    """Manage trips and their legs (flight / train / hotel / transfer / activity).

    Actions:
      add_trip       name= destination= start_date= end_date= status= notes=
      update_trip    id= [name= destination= start_date= end_date= status= notes=]
      set_status     id= status=<planning|booked|in_progress|done|cancelled>
      cancel_trip    id=
      list_trips     status= (filter)
      get_trip       id=        — trip + all legs
      upcoming       within_days= (default 90)
      add_leg        trip_id= kind= carrier= reference= from_loc= to_loc=
                     start= end= cost= currency= notes=
      update_leg     id= [any of the above fields]
      delete_leg     id=
      list_legs      trip_id=
      next_leg       [trip_id=]  — next upcoming leg overall or on one trip
      total_cost     trip_id=    — sum of leg costs per currency
    """
    _ensure()
    p = parameters or {}
    action = str(p.get("action", "")).lower().strip()

    if action == "add_trip":
        name = str(p.get("name") or "").strip()
        if not name:
            return "travel add_trip: provide 'name'."
        try:
            tid = _t.add_trip(
                name,
                destination=p.get("destination") or None,
                start_date=p.get("start_date") or None,
                end_date=p.get("end_date") or None,
                status=str(p.get("status") or "planning"),
                notes=p.get("notes") or None,
            )
        except ValueError as e:
            return f"travel add_trip: {e}"
        return f"Trip #{tid} created: {name}"

    if action == "update_trip":
        tid = _int(p.get("id"))
        if tid is None:
            return "travel update_trip: provide numeric 'id'."
        patch = {k: p[k] for k in
                 ("name", "destination", "start_date", "end_date",
                  "status", "notes")
                 if k in p and p[k] is not None}
        return (f"Updated trip #{tid}."
                if _t.update_trip(tid, **patch)
                else f"Trip #{tid} not found.")

    if action == "set_status":
        tid = _int(p.get("id"))
        status = str(p.get("status") or "").lower()
        if tid is None or status not in _t.TRIP_STATUSES:
            return (f"travel set_status: provide 'id' and status in "
                    f"{', '.join(_t.TRIP_STATUSES)}.")
        return (f"Trip #{tid} → {status}."
                if _t.update_trip(tid, status=status)
                else f"Trip #{tid} not found.")

    if action == "cancel_trip":
        tid = _int(p.get("id"))
        if tid is None:
            return "travel cancel_trip: provide 'id'."
        return (f"Cancelled trip #{tid}."
                if _t.cancel_trip(tid)
                else f"Trip #{tid} not found.")

    if action == "list_trips":
        rows = _t.list_trips(status=p.get("status") or None)
        if not rows:
            return "No trips."
        return (f"Trips ({len(rows)}):\n"
                + "\n".join(_fmt_trip(r) for r in rows))

    if action == "get_trip":
        tid = _int(p.get("id"))
        if tid is None:
            return "travel get_trip: provide 'id'."
        trip = _t.get_trip(tid)
        if not trip:
            return f"Trip #{tid} not found."
        lines = [_fmt_trip(trip)]
        legs = _t.list_legs(tid)
        if legs:
            lines.append("Legs:")
            for leg in legs:
                lines.append(_fmt_leg(leg))
        else:
            lines.append("  (no legs yet)")
        totals = _t.trip_total_cost(tid)
        if totals:
            lines.append("Total: " + ", ".join(
                f"{c} {v:,.2f}" for c, v in totals.items()
            ))
        return "\n".join(lines)

    if action == "upcoming":
        try:
            days = int(p.get("within_days") or 90)
        except (TypeError, ValueError):
            days = 90
        rows = _t.upcoming_trips(within_days=days)
        if not rows:
            return f"No trips within {days} day(s)."
        return (f"Upcoming trips ({len(rows)}):\n"
                + "\n".join(_fmt_trip(r) for r in rows))

    if action == "add_leg":
        tid = _int(p.get("trip_id"))
        if tid is None:
            return "travel add_leg: provide 'trip_id'."
        lid = _t.add_leg(
            tid,
            kind=str(p.get("kind") or "note"),
            carrier=p.get("carrier") or None,
            reference=p.get("reference") or None,
            from_loc=p.get("from_loc") or p.get("from") or None,
            to_loc=p.get("to_loc") or p.get("to") or None,
            start_ts=p.get("start") or p.get("start_ts"),
            end_ts=p.get("end") or p.get("end_ts"),
            cost=p.get("cost"),
            currency=p.get("currency") or None,
            notes=p.get("notes") or None,
        )
        return f"Added leg #{lid} to trip #{tid}."

    if action == "update_leg":
        lid = _int(p.get("id"))
        if lid is None:
            return "travel update_leg: provide 'id'."
        patch = {}
        for k in ("kind", "carrier", "reference", "from_loc", "to_loc",
                  "notes", "currency"):
            if k in p and p[k] is not None:
                patch[k] = p[k]
        if "start" in p and p["start"] is not None:
            patch["start_ts"] = p["start"]
        if "end" in p and p["end"] is not None:
            patch["end_ts"] = p["end"]
        if "cost" in p and p["cost"] is not None and p["cost"] != "":
            patch["cost"] = p["cost"]
        return (f"Updated leg #{lid}."
                if _t.update_leg(lid, **patch)
                else f"Leg #{lid} not found.")

    if action == "delete_leg":
        lid = _int(p.get("id"))
        if lid is None:
            return "travel delete_leg: provide 'id'."
        return (f"Deleted leg #{lid}."
                if _t.delete_leg(lid)
                else f"Leg #{lid} not found.")

    if action == "list_legs":
        tid = _int(p.get("trip_id"))
        if tid is None:
            return "travel list_legs: provide 'trip_id'."
        legs = _t.list_legs(tid)
        if not legs:
            return f"No legs on trip #{tid}."
        return "\n".join(_fmt_leg(l) for l in legs)

    if action == "next_leg":
        tid = _int(p.get("trip_id"))
        leg = _t.next_leg(trip_id=tid)
        if not leg:
            return "No upcoming leg found."
        return _fmt_leg(leg)

    if action == "total_cost":
        tid = _int(p.get("trip_id"))
        if tid is None:
            return "travel total_cost: provide 'trip_id'."
        totals = _t.trip_total_cost(tid)
        if not totals:
            return f"No costs recorded on trip #{tid}."
        return "Total: " + ", ".join(
            f"{c} {v:,.2f}" for c, v in totals.items()
        )

    return (f"travel: unknown action {action!r}. "
            "Actions: add_trip | update_trip | set_status | cancel_trip | "
            "list_trips | get_trip | upcoming | add_leg | update_leg | "
            "delete_leg | list_legs | next_leg | total_cost")


TOOL = {
    "name": "travel",
    "description": (
        "Manage trips and their legs (flights, trains, hotels, transfers, "
        "activities, generic notes). Local store only — nothing books, "
        "nothing pays. Pair with flight_finder for research and with "
        "finance for expense tracking (the planner can push each booked "
        "leg's cost into finance.add_expense on confirmation). 'next_leg' "
        "returns the next upcoming segment across all trips so the voice "
        "loop can say 'your flight to Tokyo departs in 6 hours'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action":      {"type": "STRING", "description": "see docstring"},
            "id":          {"type": "INTEGER", "description": "Trip or leg id."},
            "trip_id":     {"type": "INTEGER", "description": "Parent trip id for leg actions."},
            "name":        {"type": "STRING",  "description": "Trip name."},
            "destination": {"type": "STRING",  "description": "Destination city/country."},
            "start_date":  {"type": "STRING",  "description": "Trip start date YYYY-MM-DD."},
            "end_date":    {"type": "STRING",  "description": "Trip end date YYYY-MM-DD."},
            "status":      {"type": "STRING",  "description": "Trip status."},
            "notes":       {"type": "STRING",  "description": "Freeform notes."},
            "kind":        {"type": "STRING",  "description": "Leg kind: flight | train | bus | car_rental | hotel | transfer | activity | note"},
            "carrier":     {"type": "STRING",  "description": "Airline / hotel brand / rail operator."},
            "reference":   {"type": "STRING",  "description": "Confirmation / PNR / booking code."},
            "from_loc":    {"type": "STRING",  "description": "Origin (airport code or city)."},
            "to_loc":      {"type": "STRING",  "description": "Destination (airport code or city)."},
            "start":       {"type": "STRING",  "description": "Leg start YYYY-MM-DD HH:MM."},
            "end":         {"type": "STRING",  "description": "Leg end YYYY-MM-DD HH:MM."},
            "cost":        {"type": "NUMBER",  "description": "Leg cost."},
            "currency":    {"type": "STRING",  "description": "ISO currency code for the leg cost."},
            "within_days": {"type": "INTEGER", "description": "Horizon for 'upcoming' (default 90)."},
        },
        "required": ["action"],
    },
    "handler": travel,
}
