"""
core/risk.py — risk aggregation over existing signals.

Reads the authoritative data the project already keeps (tasks, schedules,
journal, finance preferences) and synthesises a short list of risk items
the assistant can surface or the dashboard can render.

Not a probabilistic model. Each rule is explicit, inspectable, and
documents the exact evidence it saw — if the user disputes a risk, they
can see every signal that contributed. This matches the project's
"explainable over black-box" preference.

Public surface:
    evaluate(*, now=None) -> list[Risk]
    Risk.to_dict() -> dict
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


# Severity labels. Mapped to a numeric probability in [0,1] when the rule
# has a basis for one; otherwise left as a qualitative tag.
_SEVERITY_ORDER = {"low": 1, "medium": 2, "high": 3}


@dataclass
class Risk:
    category: str                    # project | deadline | budget | workflow | infra
    title: str                       # one-line description
    severity: str                    # low | medium | high
    probability: Optional[float] = None    # [0,1], None when rule-based only
    impact: str = "medium"           # low | medium | high
    horizon_s: Optional[int] = None  # how far ahead this risk is
    evidence: list[dict] = field(default_factory=list)
    recommendation: str = ""
    source: str = ""                 # which rule produced the risk

    def to_dict(self) -> dict:
        return {
            "category":       self.category,
            "title":          self.title,
            "severity":       self.severity,
            "probability":    self.probability,
            "impact":         self.impact,
            "horizon_s":      self.horizon_s,
            "evidence":       self.evidence,
            "recommendation": self.recommendation,
            "source":         self.source,
        }


# ── Rules ──────────────────────────────────────────────────────────────

def _rule_overdue_tasks(now: float) -> list[Risk]:
    try:
        from core import tasks as _t
    except Exception:
        return []
    # Only connect if the caller / main.py hasn't already — tests use a
    # tmp_path connection we must not clobber.
    if getattr(_t, "_DB", None) is None:
        db_path = (Path(__file__).resolve().parent.parent
                   / "memory" / "tasks.sqlite")
        try:
            _t.connect(db_path)
        except Exception:
            return []
    out: list[Risk] = []
    try:
        rows = _t.list_tasks(status=["running", "awaiting_user"], limit=200)
    except Exception:
        return out
    for r in rows:
        age = now - float(r.get("updated_at") or now)
        # 24h without progress on a running task = soft risk; 72h = hard.
        if r.get("status") == "running" and age > 24 * 3600:
            severity = "high" if age > 72 * 3600 else "medium"
            out.append(Risk(
                category="project",
                title=f"Task #{r['id']} stalled for "
                      f"{age / 3600:.0f}h: {r.get('goal', '')[:80]}",
                severity=severity,
                impact="medium",
                evidence=[{"task_id": r["id"],
                           "last_update_age_h": round(age / 3600, 1)}],
                recommendation="Resume, reassign, or cancel.",
                source="risk.rule_overdue_tasks",
            ))
        if r.get("status") == "awaiting_user" and age > 6 * 3600:
            out.append(Risk(
                category="workflow",
                title=f"Task #{r['id']} awaiting user for "
                      f"{age / 3600:.0f}h: {r.get('goal', '')[:80]}",
                severity="medium",
                impact="low",
                evidence=[{"task_id": r["id"], "awaiting_h": round(age/3600, 1)}],
                recommendation="Approve, defer, or cancel on the HUD.",
                source="risk.rule_awaiting_user",
            ))
    return out


def _rule_schedule_conflicts(now: float) -> list[Risk]:
    """Flag scheduled items that will fire within 2 minutes of each other —
    the user is likely to get overlapping notifications or (worse) the
    worker will serialise two independent runs."""
    try:
        from core import scheduler as _sc
    except Exception:
        return []
    out: list[Risk] = []
    upcoming: list[tuple[float, dict]] = []
    try:
        for r in getattr(_sc, "list_schedules", lambda: [])():
            nxt = r.get("next_run_at")
            if nxt and nxt > now:
                upcoming.append((float(nxt), r))
    except Exception:
        return out
    upcoming.sort()
    for i in range(len(upcoming) - 1):
        gap = upcoming[i + 1][0] - upcoming[i][0]
        if gap < 120:
            out.append(Risk(
                category="deadline",
                title=f"Two scheduled items fire within {gap:.0f}s of each other",
                severity="low",
                impact="low",
                horizon_s=int(upcoming[i][0] - now),
                evidence=[
                    {"at": upcoming[i][0], "goal": upcoming[i][1].get("goal")},
                    {"at": upcoming[i + 1][0], "goal": upcoming[i + 1][1].get("goal")},
                ],
                recommendation="Reschedule one or merge the goals.",
                source="risk.rule_schedule_conflicts",
            ))
    return out


def _rule_failed_predictions(now: float) -> list[Risk]:
    """When the recent prediction MAPE on a category exceeds 50%, flag it
    — the model guiding plans for that category is probably miscalibrated."""
    try:
        from core import predictions as _p
    except Exception:
        return []
    out: list[Risk] = []
    seen_categories: set[str] = set()
    try:
        recent = _p.list_predictions(status="resolved", limit=500)
    except Exception:
        return out
    for pr in recent:
        seen_categories.add(pr.category)
    for cat in seen_categories:
        try:
            m = _p.metrics(category=cat, kind="numeric")
        except Exception:
            continue
        mape = m.get("mape")
        if mape is not None and mape > 0.5 and m.get("numeric_n", 0) >= 5:
            out.append(Risk(
                category="infra",
                title=f"Prediction category '{cat}' is poorly calibrated "
                      f"(MAPE {mape * 100:.0f}%, n={m['numeric_n']})",
                severity="medium",
                impact="medium",
                evidence=[{"category": cat, "mape": mape, "n": m["numeric_n"]}],
                recommendation=f"Re-examine the inputs feeding '{cat}' "
                               "predictions.",
                source="risk.rule_failed_predictions",
            ))
    return out


_RULES = [
    _rule_overdue_tasks,
    _rule_schedule_conflicts,
    _rule_failed_predictions,
]


# ── Public ──────────────────────────────────────────────────────────────

def evaluate(*, now: Optional[float] = None) -> list[Risk]:
    """Run every rule and return the collected risks, sorted by severity."""
    t = now or time.time()
    all_risks: list[Risk] = []
    for rule in _RULES:
        try:
            all_risks.extend(rule(t))
        except Exception as e:
            # A broken rule must not sink the whole report.
            all_risks.append(Risk(
                category="infra",
                title=f"Risk rule crashed: {rule.__name__}",
                severity="low",
                impact="low",
                evidence=[{"error": str(e)}],
                source=rule.__name__,
            ))
    all_risks.sort(key=lambda r: _SEVERITY_ORDER.get(r.severity, 0),
                   reverse=True)
    return all_risks


def register_rule(fn) -> None:
    """Allow callers (actions, plugins) to inject custom rules."""
    if fn not in _RULES:
        _RULES.append(fn)
