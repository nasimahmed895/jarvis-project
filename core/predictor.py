"""
core/predictor.py — pattern-driven task suggestions (P4.11).

Scans the journal for `task_done` / `step_start` events across the last N
days, groups them by goal / verb, and proposes a schedule entry when the
same action has fired at consistent times. No ML — a small, legible
heuristic is more trustworthy than a black box for a recommendation the
user has to explicitly accept.

Suggestion format:
    {
        "kind":         "daily" | "weekly",
        "goal":         "<normalised goal phrase>",
        "slot":         "HH:MM" | "<weekday>@HH:MM",
        "support":      <int>   # how many occurrences fed the suggestion
        "confidence":   <float in [0,1]>,
        "reason":       "<human line>"
    }

This module only returns suggestions. Scheduling is the caller's
responsibility — pass an accepted suggestion into `core/scheduler.py:Scheduler.add_cron`.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
from collections import defaultdict
from pathlib import Path
from statistics import median
from typing import Iterable

from memory.memory_manager import BASE_DIR

_JOURNAL_DIR = BASE_DIR / "memory" / "journal"

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# Occurrences below this threshold are ignored — not enough signal.
_MIN_SUPPORT = 3
# When clock skews within this many minutes, we treat them as the same slot.
_SLOT_TOLERANCE_MIN = 30


def read_history(days: int = 30, directory: Path | None = None) -> list[dict]:
    """Walk the journal directory and return events from the last `days`."""
    d = directory or _JOURNAL_DIR
    if not d.exists():
        return []
    cutoff = _dt.date.today() - _dt.timedelta(days=days)
    out: list[dict] = []
    for p in sorted(d.glob("*.jsonl")):
        try:
            day = _dt.date.fromisoformat(p.stem)
        except ValueError:
            continue
        if day < cutoff:
            continue
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                try:
                    rec = json.loads(line)
                    rec["_day"] = day.isoformat()
                    out.append(rec)
                except Exception:
                    continue
        except OSError:
            continue
    return out


def suggest(days: int = 30,
            directory: Path | None = None,
            min_support: int = _MIN_SUPPORT,
            today: _dt.date | None = None) -> list[dict]:
    """Produce suggestion dicts from recent journal history."""
    events = read_history(days=days, directory=directory)
    if not events:
        return []
    today = today or _dt.date.today()
    buckets = _group_by_goal(events)
    out: list[dict] = []
    for goal, rows in buckets.items():
        if len(rows) < min_support:
            continue
        suggestion = _distill(goal, rows, today=today, support=len(rows))
        if suggestion is not None:
            out.append(suggestion)
    out.sort(key=lambda s: (s["confidence"], s["support"]), reverse=True)
    return out


# ── Internals ────────────────────────────────────────────────────────────────

def _group_by_goal(events: Iterable[dict]) -> dict[str, list[dict]]:
    buckets: dict[str, list[dict]] = defaultdict(list)
    for e in events:
        if e.get("event") not in ("task_done", "step_start"):
            continue
        goal = _goal_from(e)
        if not goal:
            continue
        buckets[goal].append(e)
    return buckets


def _goal_from(event: dict) -> str | None:
    g = event.get("goal") or event.get("verb")
    if not isinstance(g, str):
        return None
    g = re.sub(r"\s+", " ", g).strip().lower()
    return g[:120] or None


def _distill(goal: str, rows: list[dict], *,
             today: _dt.date, support: int) -> dict | None:
    """Decide whether this goal fires at a consistent daily or weekly slot."""
    times: list[_dt.datetime] = []
    for r in rows:
        ts = r.get("ts")
        if not isinstance(ts, str):
            continue
        try:
            t = _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
            times.append(t.astimezone())
        except ValueError:
            continue
    if len(times) < support:
        # Some rows had unparseable timestamps.
        support = len(times)
        if support < _MIN_SUPPORT:
            return None

    # Daily pattern: events cluster at the same minute-of-day regardless of
    # weekday. Compute median minute + spread; accept if spread ≤ tolerance.
    minutes = [t.hour * 60 + t.minute for t in times]
    med = int(median(minutes))
    spread = max(abs(m - med) for m in minutes)
    distinct_weekdays = {t.weekday() for t in times}
    if spread <= _SLOT_TOLERANCE_MIN and len(distinct_weekdays) >= 3:
        hh, mm = divmod(med, 60)
        confidence = 1.0 - spread / (_SLOT_TOLERANCE_MIN + 1)
        return {
            "kind":       "daily",
            "goal":       goal,
            "slot":       f"{hh:02d}:{mm:02d}",
            "cron":       f"{mm} {hh} * * *",
            "support":    support,
            "confidence": round(confidence, 2),
            "reason":     (f"'{goal}' fired {support} times in "
                           f"{len(distinct_weekdays)} different weekdays, "
                           f"all within ±{spread}min of {hh:02d}:{mm:02d}."),
        }

    # Weekly pattern: events cluster on the same weekday + same minute-of-day.
    by_weekday: dict[int, list[int]] = defaultdict(list)
    for t in times:
        by_weekday[t.weekday()].append(t.hour * 60 + t.minute)
    best_wd: int | None = None
    best_rows: list[int] = []
    for wd, mins in by_weekday.items():
        if len(mins) > len(best_rows):
            best_wd, best_rows = wd, mins
    if best_wd is None or len(best_rows) < _MIN_SUPPORT:
        return None
    med_w = int(median(best_rows))
    spread_w = max(abs(m - med_w) for m in best_rows)
    if spread_w > _SLOT_TOLERANCE_MIN:
        return None
    hh, mm = divmod(med_w, 60)
    confidence = (len(best_rows) / support) * (1.0 - spread_w / (_SLOT_TOLERANCE_MIN + 1))
    # cron day-of-week: Sunday=0..Saturday=6; Python Mon=0..Sun=6.
    dow = (best_wd + 1) % 7
    return {
        "kind":       "weekly",
        "goal":       goal,
        "slot":       f"{_WEEKDAYS[best_wd]}@{hh:02d}:{mm:02d}",
        "cron":       f"{mm} {hh} * * {dow}",
        "support":    len(best_rows),
        "confidence": round(confidence, 2),
        "reason":     (f"'{goal}' fired {len(best_rows)} times on "
                       f"{_WEEKDAYS[best_wd]}, all within ±{spread_w}min "
                       f"of {hh:02d}:{mm:02d}."),
    }
