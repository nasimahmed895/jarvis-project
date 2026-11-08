"""
actions/productivity_report.py — deterministic analytics rollup (P3.2).

Pure data, no LLM. Reads the existing stores:
    - tasks (`core/tasks.py`)
    - commitments (`core/commitments.py`)
    - habits (`actions/habit.py`)
    - journal (`core/journal.py`) — step duration averages

Produces an ASCII / markdown report for a user-chosen window
(`day` / `week` / `month`). Separate from `actions/report_gen.py`, which
LLM-writes narrative reports — this one is for numbers the user can trust.
"""
from __future__ import annotations

import datetime as _dt
import time
from typing import Any

from core import tasks as _tasks

try:
    from core import commitments as _comm
    _HAS_COMM = True
except Exception:
    _HAS_COMM = False

try:
    from actions import habit as _habit
    _HAS_HABIT = True
except Exception:
    _HAS_HABIT = False


_WINDOWS = {
    "day":   _dt.timedelta(days=1),
    "week":  _dt.timedelta(days=7),
    "month": _dt.timedelta(days=30),
}


def rollup(window: str = "week", *, now: float | None = None) -> dict:
    """Return a plain dict covering `window` back from `now` (unix seconds)."""
    if window not in _WINDOWS:
        raise ValueError(f"window must be one of {sorted(_WINDOWS)}, got {window!r}")
    now_ts = now if now is not None else time.time()
    start_ts = now_ts - _WINDOWS[window].total_seconds()
    return {
        "window":   window,
        "start":    start_ts,
        "end":      now_ts,
        "tasks":    _task_stats(start_ts, now_ts),
        "commits":  _commitment_stats(start_ts, now_ts) if _HAS_COMM else None,
        "habits":   _habit_stats() if _HAS_HABIT else None,
    }


def _task_stats(start_ts: float, end_ts: float) -> dict:
    try:
        rows = _tasks.list_tasks(limit=500)
    except Exception:
        return {"note": "tasks store unavailable"}
    by_status: dict[str, int] = {}
    durations: list[float] = []
    for t in rows:
        created = float(t.get("created_at") or 0)
        if created < start_ts or created > end_ts:
            continue
        by_status[t["status"]] = by_status.get(t["status"], 0) + 1
        try:
            for s in _tasks.task_steps(t["id"]):
                d = s.get("duration_s")
                if d is not None and float(d) >= 0:
                    durations.append(float(d))
        except Exception:
            continue
    total = sum(by_status.values())
    done = by_status.get("done", 0)
    return {
        "created":       total,
        "by_status":     by_status,
        "completion_pct": round(100.0 * done / total, 1) if total else 0.0,
        "steps_timed":   len(durations),
        "avg_step_s":    round(sum(durations) / len(durations), 2) if durations else 0.0,
        "max_step_s":    round(max(durations), 2) if durations else 0.0,
    }


def _commitment_stats(start_ts: float, end_ts: float) -> dict:
    try:
        rows = _comm.list_all(limit=500)
    except Exception:
        return {"note": "commitments store unavailable"}
    counts: dict[str, int] = {}
    kept = 0
    missed = 0
    for c in rows:
        created = float(c.get("created_at") or 0)
        if created < start_ts or created > end_ts:
            continue
        status = c["status"]
        counts[status] = counts.get(status, 0) + 1
        if status == "done":
            kept += 1
        elif status == "missed":
            missed += 1
    resolved = kept + missed
    return {
        "opened":    sum(counts.values()),
        "by_status": counts,
        "kept_pct":  round(100.0 * kept / resolved, 1) if resolved else None,
    }


def _habit_stats() -> list[dict]:
    try:
        rows = _habit.list_habits()
    except Exception:
        return []
    return [
        {"name": r["name"], "cadence": r["cadence"], "streak": r["streak"]}
        for r in rows
    ]


# ── Rendering ────────────────────────────────────────────────────────────────

def render_markdown(stats: dict) -> str:
    start = _dt.datetime.fromtimestamp(stats["start"]).strftime("%Y-%m-%d")
    end   = _dt.datetime.fromtimestamp(stats["end"]).strftime("%Y-%m-%d")
    lines = [
        f"# Productivity rollup — {stats['window']}",
        f"_{start} → {end}_",
        "",
    ]
    t = stats["tasks"]
    lines.append("## Tasks")
    if t.get("note"):
        lines.append(f"- _{t['note']}_")
    else:
        lines.append(f"- Created: **{t['created']}**")
        lines.append(f"- Completion: **{t['completion_pct']}%**")
        for status, count in sorted(t["by_status"].items()):
            lines.append(f"  - {status}: {count}")
        if t["steps_timed"]:
            lines.append(f"- Step latency: avg {t['avg_step_s']}s, max {t['max_step_s']}s "
                         f"({t['steps_timed']} samples)")
    if stats.get("commits") is not None:
        c = stats["commits"]
        lines += ["", "## Commitments"]
        if c.get("note"):
            lines.append(f"- _{c['note']}_")
        else:
            lines.append(f"- Opened: **{c['opened']}**")
            if c["kept_pct"] is not None:
                lines.append(f"- Kept rate: **{c['kept_pct']}%**")
            for status, count in sorted(c["by_status"].items()):
                lines.append(f"  - {status}: {count}")
    habits = stats.get("habits")
    if habits is not None:
        lines += ["", "## Habits"]
        if not habits:
            lines.append("- (none tracked)")
        else:
            for h in habits:
                lines.append(f"- **{h['name']}** ({h['cadence']}) — streak {h['streak']}")
    return "\n".join(lines).rstrip() + "\n"


# ── Tool dispatch ────────────────────────────────────────────────────────────

def _tool_entry(parameters: dict, **_kw: Any) -> str:
    window = (parameters.get("window") or "week").lower()
    try:
        stats = rollup(window)
    except ValueError as e:
        return str(e)
    return render_markdown(stats)


TOOL = {
    "name": "productivity_report",
    "description": (
        "Deterministic productivity analytics for day / week / month. Counts "
        "tasks, commitments and habits from the local stores. Returns a "
        "markdown rollup — no LLM, so the numbers are exact."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "window": {"type": "string", "enum": ["day", "week", "month"]},
        },
    },
    "run": _tool_entry,
}
