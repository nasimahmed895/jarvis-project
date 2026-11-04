"""
Gatekeeper action — triage inbound items (email, meeting request, call).

Thin wrapper over ``core/gatekeeper.py`` with convenience actions:

  triage          classify ONE item from inline fields
  triage_inbox    classify a list of items (parsed JSON) and return ranked
  availability    check whether a proposed time collides with the calendar
  rules           dump the current rule tables so the model can explain itself

No DB writes. CRM is consulted read-only during classification.
"""
from __future__ import annotations

import json
import platform
from pathlib import Path
from typing import Any

from core import gatekeeper as _gk
from core import crm as _crm

_CRM_CONNECTED = False


def _ensure_crm() -> None:
    global _CRM_CONNECTED
    if _CRM_CONNECTED:
        return
    db = Path(__file__).resolve().parent.parent / "memory" / "crm.sqlite"
    try:
        _crm.connect(db)
    except Exception as e:
        print(f"[Gatekeeper] CRM not available: {e}")
    _CRM_CONNECTED = True


def _coerce_items(raw) -> list[dict]:
    """Accept either a list[dict] (natural) or a JSON string (what the
    planner usually produces). Reject anything that doesn't parse — return
    [] so the handler emits a clear error message rather than raising."""
    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, dict)]
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                return [x for x in data if isinstance(x, dict)]
        except Exception:
            return []
    return []


def _fmt_verdict(v: dict) -> str:
    bits = [v.get("priority", "?"), v.get("recommendation", "?"),
            f"src={v.get('source','?')}",
            f"tier={v.get('tier','?')}"]
    reasons = v.get("reasons") or []
    if reasons:
        bits.append("[" + ", ".join(reasons[:4]) + "]")
    return " ".join(bits)


def gatekeeper(
    parameters:    dict | None = None,
    response: Any = None,
    player:   Any = None,
    session_memory: Any = None,
) -> str:
    """Classify inbound items by executive priority.

    Actions:
      triage        kind= subject= body= sender_email= sender_name=
      triage_inbox  items=<JSON list of {subject, body, sender_email, kind}>
      availability  start=<YYYY-MM-DD HH:MM> [end= or duration_min=]
                    days=<n>   (how many days of calendar to pull)
      rules         — summarise priority rules
    """
    _ensure_crm()
    params = parameters or {}
    action = str(params.get("action", "triage")).lower().strip()

    if action == "triage":
        kind = str(params.get("kind") or "email").strip() or "email"
        v = _gk.classify(
            kind=kind,
            subject=params.get("subject"),
            body=params.get("body"),
            sender_email=params.get("sender_email") or params.get("from"),
            sender_name=params.get("sender_name") or params.get("name"),
        )
        d = v.to_dict()
        return (
            f"Verdict: {d['priority']} / {d['recommendation']} "
            f"(source={d['source']}, tier={d['tier']})\n"
            f"Reasons: {', '.join(d['reasons'])}"
        )

    if action == "triage_inbox":
        items = _coerce_items(params.get("items"))
        if not items:
            return "gatekeeper triage_inbox: provide 'items' as a JSON list."
        try:
            limit = int(params.get("limit", 20))
        except (TypeError, ValueError):
            limit = 20
        ranked = _gk.priority_queue(items, limit=limit)
        if not ranked:
            return "No items to triage."
        lines = [f"Triaged {len(ranked)} item(s):"]
        for r in ranked:
            v = r.get("verdict") or {}
            subj = (r.get("subject") or "").strip()[:80]
            frm = r.get("sender_email") or r.get("from") or "?"
            lines.append(f"  {_fmt_verdict(v)} — {frm} · {subj}")
        return "\n".join(lines)

    if action == "availability":
        start = str(params.get("start") or "").strip()
        if not start:
            return "gatekeeper availability: provide 'start' (YYYY-MM-DD HH:MM)."
        end = params.get("end")
        if not end:
            try:
                dur = int(params.get("duration_min") or 60)
            except (TypeError, ValueError):
                dur = 60
            # Compute a rough end-time string with pure stdlib, no tz.
            from datetime import datetime, timedelta
            for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
                try:
                    s = datetime.strptime(start, fmt)
                    end = (s + timedelta(minutes=dur)).strftime("%Y-%m-%d %H:%M")
                    break
                except ValueError:
                    continue
            else:
                return "gatekeeper availability: 'start' not parseable."

        events: list[dict] = []
        if platform.system() == "Darwin":
            try:
                from actions import _mac_apps as _ap
                try:
                    days = int(params.get("days") or 14)
                except (TypeError, ValueError):
                    days = 14
                events = _ap.calendar_list_events(
                    days=days, calendar_name=params.get("calendar"),
                ) or []
            except Exception as e:
                return (f"gatekeeper availability: could not read calendar "
                        f"({type(e).__name__}: {e}).")
        else:
            return ("gatekeeper availability: calendar pull is macOS-only "
                    "in this build.")

        conflicts = _gk.detect_conflict(events, start, end)
        if not conflicts:
            return f"Free {start} → {end}. No calendar conflicts."
        lines = [f"Conflicts between {start} and {end}:"]
        for ev in conflicts[:10]:
            loc = f" @ {ev.get('location')}" if ev.get("location") else ""
            lines.append(f"  [{ev.get('calendar','?')}] "
                         f"{ev.get('start','?')} — "
                         f"{ev.get('title','(untitled)')}{loc}")
        return "\n".join(lines)

    if action == "rules":
        lines = ["Priority rules:"]
        for tier, pri in _gk._TIER_BASE_PRIORITY.items():
            lines.append(f"  tier={tier:<10} → base {pri}")
        lines.append("Bumpers: urgency keywords (+1), red-flag keywords (→P0).")
        lines.append("Lowerers: promo/unsubscribe keywords on UNKNOWN senders (-1).")
        lines.append("Recommendations: P0→escalate, P1→flag, "
                     "P2→flag(known)/normal(unknown), P3→normal, "
                     "promo+unknown→auto_decline.")
        return "\n".join(lines)

    return (f"gatekeeper: unknown action {action!r}. "
            "Actions: triage | triage_inbox | availability | rules")


TOOL = {
    "name": "gatekeeper",
    "description": (
        "Triage inbound items by executive priority. Use 'triage' when the "
        "user forwards one email/message and asks 'is this important?'. Use "
        "'triage_inbox' when they ask 'what should I look at first?' and "
        "pass the list of items you already fetched (with email_manager). "
        "'availability' checks whether a proposed meeting time conflicts "
        "with the calendar. Classification combines CRM tier (VIP, client, "
        "investor…) with urgency/red-flag/promo keywords; it is offline "
        "and fast, safe to call on every inbox item. P0 means interrupt, "
        "P1 means flag today, P2 means this week, P3 means later; the "
        "recommendation field tells you the suggested next move."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "triage | triage_inbox | availability | rules",
            },
            "kind":          {"type": "STRING", "description": "email | message | meeting_request | call"},
            "subject":       {"type": "STRING", "description": "Subject line (triage)."},
            "body":          {"type": "STRING", "description": "Message body (triage)."},
            "sender_email":  {"type": "STRING", "description": "Sender email address."},
            "sender_name":   {"type": "STRING", "description": "Sender display name."},
            "items":         {"type": "STRING", "description": "JSON list of items for triage_inbox."},
            "limit":         {"type": "INTEGER", "description": "Max ranked items to return (default 20)."},
            "start":         {"type": "STRING", "description": "Availability check start — YYYY-MM-DD HH:MM."},
            "end":           {"type": "STRING", "description": "Availability check end — YYYY-MM-DD HH:MM (or supply duration_min)."},
            "duration_min":  {"type": "INTEGER", "description": "Duration in minutes if 'end' not set (default 60)."},
            "days":          {"type": "INTEGER", "description": "Days of calendar to scan for 'availability' (default 14)."},
            "calendar":      {"type": "STRING", "description": "Calendar name filter for 'availability'."},
        },
        "required": ["action"],
    },
    "handler": gatekeeper,
}
