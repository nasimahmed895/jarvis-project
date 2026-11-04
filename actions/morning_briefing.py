"""
Morning briefing — one-shot aggregated digest of what matters today.

Pulls from the stores built up by the other actions:

  - Calendar events for the day (macOS Calendar via _mac_apps)
  - Due commitments (core/commitments.py)
  - Due follow-ups (core/crm.py)
  - Upcoming bills (core/finance.py)
  - Budget status for the current month
  - Active projects with their next milestone
  - Priority emails if a list has been pre-fetched and passed in
  - News headlines via web_search (optional, best-effort)
  - Weather (best-effort hook — degrades gracefully if the action
    isn't installed or can't locate a city)

Deterministic by default — no LLM. Pass ``use_model=1`` to get a
one-paragraph opening written by Gemini summarising the key items.
"""
from __future__ import annotations

import datetime as _dt
import json
import platform
from pathlib import Path
from typing import Any

from core import commitments as _c
from core import crm as _crm
from core import finance as _fin
from core import projects as _proj
try:
    from core import gatekeeper as _gk
except Exception:
    _gk = None  # type: ignore[assignment]
try:
    from core import gemini as _gem
except Exception:
    _gem = None  # type: ignore[assignment]


_STORES_READY = False


def _ensure_stores() -> None:
    global _STORES_READY
    if _STORES_READY:
        return
    base = Path(__file__).resolve().parent.parent / "memory"
    for mod, name in (
        (_c, "commitments.sqlite"),
        (_crm, "crm.sqlite"),
        (_fin, "finance.sqlite"),
        (_proj, "projects.sqlite"),
    ):
        try:
            mod.connect(base / name)
        except Exception as e:
            print(f"[MorningBriefing] {name} unavailable: {e}")
    _STORES_READY = True


# ── Section builders ─────────────────────────────────────────────────────────

def _section_schedule(days: int = 1) -> list[str]:
    if platform.system() != "Darwin":
        return []
    try:
        from actions import _mac_apps as _ap
        events = _ap.calendar_list_events(days=days) or []
    except Exception as e:
        print(f"[MorningBriefing] calendar error: {e}")
        return []
    if not events:
        return []
    today = _dt.date.today().strftime("%Y-%m-%d")
    todays = [e for e in events if (e.get("start", "") or "").startswith(today)]
    source = todays or events[:5]
    lines = ["## Today's schedule"]
    for ev in source:
        loc = f" @ {ev.get('location')}" if ev.get("location") else ""
        lines.append(f"  - {ev.get('start', '?')} — "
                     f"{ev.get('title', '(untitled)')}{loc}")
    return lines


def _section_commitments(hours: float = 24.0) -> list[str]:
    try:
        rows = _c.due_within(hours)
    except Exception as e:
        print(f"[MorningBriefing] commitments error: {e}")
        return []
    if not rows:
        return []
    out = [f"## Commitments due within {int(hours)}h"]
    for r in rows:
        due = ""
        if r.get("due_ts"):
            due = " — due " + _dt.datetime.fromtimestamp(
                float(r["due_ts"])).strftime("%Y-%m-%d %H:%M")
        out.append(f"  - #{r['id']} {r['text']}{due}")
    return out


def _section_follow_ups(hours: float = 48.0) -> list[str]:
    try:
        rows = _crm.due_follow_ups(within_hours=hours)
    except Exception as e:
        print(f"[MorningBriefing] follow-ups error: {e}")
        return []
    if not rows:
        return []
    out = [f"## Follow-ups due within {int(hours)}h"]
    for r in rows:
        who = r.get("contact_name") or "(no contact)"
        tier = r.get("contact_tier") or ""
        tag = f"[{tier}] " if tier else ""
        due = ""
        if r.get("due_ts"):
            due = " — due " + _dt.datetime.fromtimestamp(
                float(r["due_ts"])).strftime("%Y-%m-%d")
        out.append(f"  - {tag}{who}: {r['text']}{due}")
    return out


def _section_bills(days: int = 7) -> list[str]:
    try:
        rows = _fin.upcoming_bills(within_days=days, include_overdue=True)
    except Exception as e:
        print(f"[MorningBriefing] bills error: {e}")
        return []
    if not rows:
        return []
    out = [f"## Bills within {days} day(s)"]
    for r in rows:
        out.append(f"  - {r['due_date']} {r['name']} "
                   f"{r['currency']} {float(r['amount']):,.2f}")
    return out


def _section_budget() -> list[str]:
    try:
        rows = _fin.budget_status()
    except Exception as e:
        print(f"[MorningBriefing] budget error: {e}")
        return []
    if not rows:
        return []
    out = ["## Budget status (this month)"]
    for r in rows:
        flag = "OVER" if r["over"] else "ok"
        out.append(f"  - [{flag}] {r['category']} ({r['currency']}): "
                   f"{r['spent']:,.2f} / {r['budget']:,.2f} "
                   f"({r['pct']*100:.0f}%)")
    return out


def _section_projects() -> list[str]:
    try:
        rows = _proj.list_projects(status=("active", "planning"))
    except Exception as e:
        print(f"[MorningBriefing] projects error: {e}")
        return []
    if not rows:
        return []
    out = ["## Active projects"]
    for r in rows:
        s = _proj.project_summary(r["id"])
        pct = s.get("completion_percent", 0)
        nxt = s.get("next_milestone") or {}
        nxt_bit = ""
        if nxt:
            nxt_bit = (f" — next: {nxt.get('name','?')} "
                       f"(target {nxt.get('target_date','?')})")
        out.append(f"  - #{r['id']} [{r['status']}] {r['name']} "
                   f"({pct}%) "
                   f"{nxt_bit}".rstrip())
    return out


def _section_emails(items_raw: Any) -> list[str]:
    """Optional: caller may pre-fetch inbox items (e.g. from
    email_manager) and pass them as a JSON string. The gatekeeper
    classifies them and we surface the top P0/P1 handful."""
    if _gk is None or not items_raw:
        return []
    items: list[dict]
    if isinstance(items_raw, str):
        try:
            data = json.loads(items_raw)
            if isinstance(data, list):
                items = [x for x in data if isinstance(x, dict)]
            else:
                return []
        except Exception:
            return []
    elif isinstance(items_raw, list):
        items = [x for x in items_raw if isinstance(x, dict)]
    else:
        return []
    ranked = _gk.priority_queue(items, limit=20)
    hot = [r for r in ranked if (r.get("verdict") or {})
           .get("priority") in ("P0", "P1")]
    if not hot:
        return []
    out = ["## Priority inbox"]
    for r in hot:
        v = r.get("verdict") or {}
        subj = (r.get("subject") or "").strip()[:80]
        who = r.get("sender_email") or r.get("from") or "?"
        out.append(f"  - {v.get('priority','?')} / {v.get('recommendation','?')} "
                   f"· {who} · {subj}")
    return out


def _section_news(query: str | None) -> list[str]:
    if not query:
        return []
    try:
        from actions import web_search as _ws
    except Exception as e:
        print(f"[MorningBriefing] web_search unavailable: {e}")
        return []
    try:
        news = _ws._news(query, max_results=4)  # noqa: SLF001
    except Exception as e:
        print(f"[MorningBriefing] news error: {e}")
        return []
    if not news:
        return []
    out = [f"## News — {query}"]
    for item in news[:4]:
        if isinstance(item, dict):
            title = (item.get("title") or "").strip()
            src = (item.get("source") or "")
            url = item.get("url") or ""
            out.append(f"  - {title} ({src})  {url}".rstrip())
        else:
            out.append(f"  - {item}")
    return out


def _section_weather(city: str | None) -> list[str]:
    if not city:
        return []
    try:
        from actions import weather_report as _wx
    except Exception:
        return []
    # weather_report.py exposes a handler with a parameters dict; call via
    # the same convention the action_loader uses.
    try:
        handler = getattr(_wx, "weather_report", None)
        if not callable(handler):
            return []
        out = handler({"location": city})
    except Exception as e:
        print(f"[MorningBriefing] weather error: {e}")
        return []
    if not out or not isinstance(out, str):
        return []
    return ["## Weather", f"  {out.strip()}"]


# ── Opening paragraph (optional LLM) ────────────────────────────────────────

def _model_intro(sections: dict[str, list[str]]) -> str | None:
    if _gem is None or not getattr(_gem, "api_key", lambda: "")():
        return None
    # Build a compact dossier for the model and ask for a single
    # sentence per section, max 90 words total.
    dossier_lines: list[str] = []
    for key, lines in sections.items():
        if not lines:
            continue
        dossier_lines.append(f"[{key}]")
        dossier_lines.extend(lines[:6])
    if not dossier_lines:
        return None
    try:
        raw = _gem.as_json(
            [
                "Write a warm, one-paragraph morning briefing (max 90 words) "
                "based on the dossier below. Highlight what matters most "
                "today, call out anything OVER budget or P0 by name. "
                "Return JSON: {\"paragraph\": \"<text>\"}",
                "\n".join(dossier_lines),
            ],
            tier=getattr(_gem, "SMART", None),
            timeout_ms=15_000,
            default=None,
        )
    except Exception:
        return None
    if isinstance(raw, dict):
        text = str(raw.get("paragraph") or "").strip()
        return text or None
    return None


# ── Entry point ─────────────────────────────────────────────────────────────

def morning_briefing(
    parameters:    dict | None = None,
    response: Any = None,
    player:   Any = None,
    session_memory: Any = None,
) -> str:
    """Build the morning briefing. Sections are included only when they
    have content, so an empty day produces a short message rather than
    a wall of placeholders.

    Fields (all optional):
      inbox_items    JSON list of pre-fetched email items (see gatekeeper)
      news_query     topic string for a 4-headline news block
      city           location for the weather hook (best-effort)
      hours          commitment window in hours (default 24)
      follow_hours   follow-up window in hours (default 48)
      bills_days     bills horizon in days (default 7)
      use_model      '1' to prepend a Gemini-written intro paragraph
    """
    _ensure_stores()
    p = parameters or {}
    try:
        hours = float(p.get("hours", 24))
    except (TypeError, ValueError):
        hours = 24.0
    try:
        follow_hours = float(p.get("follow_hours", 48))
    except (TypeError, ValueError):
        follow_hours = 48.0
    try:
        bills_days = int(p.get("bills_days", 7))
    except (TypeError, ValueError):
        bills_days = 7

    sections: dict[str, list[str]] = {
        "schedule":    _section_schedule(),
        "commitments": _section_commitments(hours),
        "follow_ups":  _section_follow_ups(follow_hours),
        "bills":       _section_bills(bills_days),
        "budget":      _section_budget(),
        "projects":    _section_projects(),
        "emails":      _section_emails(p.get("inbox_items")),
        "news":        _section_news(p.get("news_query")),
        "weather":     _section_weather(p.get("city")),
    }

    today = _dt.date.today().strftime("%A, %B %d, %Y")
    out = [f"# Morning briefing — {today}"]
    content_any = any(lines for lines in sections.values())
    if not content_any:
        out.append("")
        out.append("Nothing on today. Looking light.")
        return "\n".join(out)

    use_model = str(p.get("use_model", "")).strip() in ("1", "true", "yes")
    if use_model:
        intro = _model_intro(sections)
        if intro:
            out.append("")
            out.append(intro)

    for key in ("schedule", "commitments", "follow_ups", "emails",
                "bills", "budget", "projects", "weather", "news"):
        lines = sections.get(key) or []
        if not lines:
            continue
        out.append("")
        out.extend(lines)
    return "\n".join(out)


TOOL = {
    "name": "morning_briefing",
    "description": (
        "Build the user's morning briefing: today's calendar, due "
        "commitments, due follow-ups, upcoming bills, this-month "
        "budget status, active projects + next milestones, optionally "
        "ranked priority emails (pass inbox_items as JSON), optional "
        "news headlines (news_query) and weather (city). Sections with "
        "no content are omitted. Pass use_model=1 to prepend a short "
        "Gemini-written opening sentence; deterministic otherwise."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "inbox_items":   {"type": "STRING", "description": "JSON list of {subject, body, sender_email, kind} for inbox triage."},
            "news_query":    {"type": "STRING", "description": "Topic for the news block (e.g. 'AI startups')."},
            "city":          {"type": "STRING", "description": "City for the weather block."},
            "hours":         {"type": "NUMBER", "description": "Commitment window (hours)."},
            "follow_hours":  {"type": "NUMBER", "description": "Follow-up window (hours)."},
            "bills_days":    {"type": "INTEGER", "description": "Bills horizon (days)."},
            "use_model":     {"type": "STRING", "description": "'1' to add a model-written opening paragraph."},
        },
        "required": [],
    },
    "handler": morning_briefing,
}
