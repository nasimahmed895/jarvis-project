"""
actions/forecast.py — expose the predictive stack to Gemini as one tool.

Three verbs, each a thin adapter:
  * suggest   → core/predictor.suggest (schedule-pattern hints)
  * risks     → core/risk.evaluate
  * scenario  → core/scenarios.{percent_change, shift_deadline, ...}
  * record    → core/predictions.record (the model tells us what it just
                predicted so the ledger can score it later)
  * resolve   → core/predictions.resolve
  * metrics   → core/predictions.metrics
"""
from __future__ import annotations

import datetime as _dt
import json
from typing import Any

from core import predictor as _predictor
from core import risk as _risk
from core import scenarios as _scenarios
from core import predictions as _preds


def _as_iso(ts: float) -> str:
    try:
        return _dt.datetime.fromtimestamp(ts).isoformat(timespec="minutes")
    except Exception:
        return str(ts)


def forecast(parameters: dict | None = None,
             response=None, player=None, speak=None,
             session_memory=None) -> str:
    params = parameters or {}
    verb = (params.get("action") or "").lower().strip()

    if verb == "suggest":
        days = int(params.get("days") or 30)
        suggestions = _predictor.suggest(days=days)
        if not suggestions:
            return "No recurring patterns with enough support yet."
        lines = ["Suggested recurring schedules:"]
        for s in suggestions:
            lines.append(
                f"- {s.get('kind')} {s.get('slot')} → {s.get('goal')} "
                f"(support={s.get('support')}, conf={s.get('confidence'):.2f})"
            )
        return "\n".join(lines)

    if verb == "risks":
        risks = _risk.evaluate()
        if not risks:
            return "No risks surfaced by current rules."
        lines = ["Current risk register:"]
        for r in risks[:int(params.get("limit", 10))]:
            lines.append(
                f"- [{r.severity.upper()}] {r.title} — {r.recommendation}"
            )
        return "\n".join(lines)

    if verb == "scenario":
        shape = (params.get("shape") or "").lower().strip()
        if shape == "percent_change":
            out = _scenarios.percent_change(
                baseline=float(params.get("baseline", 0)),
                pct=float(params.get("pct", 0)),
                uncertainty=float(params.get("uncertainty", 0.2)),
            )
            return json.dumps(out.to_dict(), indent=2)
        if shape == "shift_deadline":
            deadline_str = (params.get("deadline") or "").strip()
            if not deadline_str:
                return "Provide 'deadline' as YYYY-MM-DD."
            try:
                d = _dt.date.fromisoformat(deadline_str)
            except ValueError:
                return f"Bad deadline: {deadline_str}"
            return json.dumps(_scenarios.shift_deadline(
                d, int(params.get("delta_days", 0)),
                int(params.get("uncertainty_days", 0))), indent=2)
        if shape == "delay_task":
            return json.dumps(_scenarios.delay_task(
                remaining_hours=float(params.get("remaining_hours", 0)),
                additional_hours=float(params.get("additional_hours", 0)),
                velocity_hours_per_day=float(
                    params.get("velocity_hours_per_day", 6)),
                uncertainty=float(params.get("uncertainty", 0.25))),
                indent=2)
        if shape == "demand_change":
            return json.dumps(_scenarios.demand_change(
                current_demand=float(params.get("current_demand", 0)),
                factor=float(params.get("factor", 1)),
                unit_cost=(float(params["unit_cost"])
                           if "unit_cost" in params else None),
                uncertainty=float(params.get("uncertainty", 0.2))), indent=2)
        if shape == "traffic_surge":
            return json.dumps(_scenarios.traffic_surge(
                current_rps=float(params.get("current_rps", 0)),
                multiplier=float(params.get("multiplier", 1)),
                capacity_rps=float(params.get("capacity_rps", 0)),
                uncertainty=float(params.get("uncertainty", 0.2))), indent=2)
        return ("Unknown shape. Use: percent_change | shift_deadline | "
                "delay_task | demand_change | traffic_surge.")

    if verb == "record":
        pid = _preds.record(
            category=(params.get("category") or "general").strip(),
            subject=(params.get("subject") or "").strip() or "unlabelled",
            kind=(params.get("kind") or "numeric").strip(),
            point=params.get("point"),
            lower=params.get("lower"),
            upper=params.get("upper"),
            confidence=params.get("confidence"),
            horizon_s=params.get("horizon_s"),
            model=params.get("model") or "model.ad-hoc",
            evidence=params.get("evidence") if isinstance(
                params.get("evidence"), dict) else None,
            assumptions=params.get("assumptions"),
        )
        return f"Prediction recorded as id={pid}."

    if verb == "resolve":
        pid = int(params.get("id"))
        err = _preds.resolve(pid,
                             actual=params.get("actual"),
                             actual_label=params.get("actual_label"))
        return (f"Prediction #{pid} resolved."
                + (f" Error: {err:+g}." if err is not None else ""))

    if verb == "metrics":
        m = _preds.metrics(category=params.get("category"),
                           kind=params.get("kind"))
        return json.dumps(m, indent=2)

    if verb == "list":
        rows = _preds.list_predictions(
            category=params.get("category"),
            status=params.get("status"),
            limit=int(params.get("limit", 20)))
        if not rows:
            return "No predictions on record."
        out = ["Recent predictions:"]
        for p in rows:
            out.append(
                f"- #{p.id} [{p.category}] {p.subject[:60]} "
                f"point={p.point} status={p.status}")
        return "\n".join(out)

    return ("Unknown action. Use: suggest | risks | scenario | record | "
            "resolve | metrics | list.")


TOOL = {
    "name": "forecast",
    "description": ("Predictive intelligence surface. suggest → recurring-"
                    "task hints. risks → aggregated risk register "
                    "(overdue tasks, scheduling conflicts, miscalibrated "
                    "prediction categories). scenario → what-if calculator "
                    "(percent_change / shift_deadline / delay_task / "
                    "demand_change / traffic_surge). record / resolve / "
                    "metrics / list → prediction ledger: when you make a "
                    "forecast, call `record` so the ledger can score "
                    "accuracy once the outcome is known."),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "suggest | risks | scenario | "
                                      "record | resolve | metrics | list"},
            "days":   {"type": "INTEGER",
                       "description": "History window for suggest"},
            "limit":  {"type": "INTEGER"},
            # scenario
            "shape":  {"type": "STRING",
                       "description": "percent_change | shift_deadline | "
                                      "delay_task | demand_change | "
                                      "traffic_surge"},
            "baseline":    {"type": "NUMBER"},
            "pct":         {"type": "NUMBER"},
            "uncertainty": {"type": "NUMBER"},
            "deadline":    {"type": "STRING",
                            "description": "YYYY-MM-DD"},
            "delta_days":       {"type": "INTEGER"},
            "uncertainty_days": {"type": "INTEGER"},
            "remaining_hours":  {"type": "NUMBER"},
            "additional_hours": {"type": "NUMBER"},
            "velocity_hours_per_day": {"type": "NUMBER"},
            "current_demand":   {"type": "NUMBER"},
            "factor":           {"type": "NUMBER"},
            "unit_cost":        {"type": "NUMBER"},
            "current_rps":      {"type": "NUMBER"},
            "multiplier":       {"type": "NUMBER"},
            "capacity_rps":     {"type": "NUMBER"},
            # prediction ledger
            "category":     {"type": "STRING"},
            "subject":      {"type": "STRING"},
            "kind":         {"type": "STRING",
                             "description": "numeric | probability | label"},
            "point":        {"type": "NUMBER"},
            "lower":        {"type": "NUMBER"},
            "upper":        {"type": "NUMBER"},
            "confidence":   {"type": "NUMBER"},
            "horizon_s":    {"type": "INTEGER"},
            "model":        {"type": "STRING"},
            "evidence":     {"type": "OBJECT", "properties": {}},
            "assumptions":  {"type": "STRING"},
            "id":           {"type": "INTEGER"},
            "actual":       {"type": "NUMBER"},
            "actual_label": {"type": "STRING"},
            "status":       {"type": "STRING",
                             "description": "open | resolved | cancelled"},
        },
        "required": ["action"],
    },
    "handler": forecast,
}
