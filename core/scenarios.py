"""
core/scenarios.py — what-if calculators.

Pure arithmetic. No I/O, no model calls. The caller hands over a baseline
(numbers, dates, series) and a *change*; this module returns optimistic /
expected / pessimistic variants using transparent rules:

  * expected    — baseline with the change applied
  * optimistic  — change * (1 - uncertainty_pct)
  * pessimistic — change * (1 + uncertainty_pct)

Uncertainty defaults to 20% but is caller-controlled so a confident input
(e.g. "the lease rate goes up 3%, this is in the contract") can be marked
`uncertainty=0`.

Nothing in here is a substitute for a model. Think of it as the "if I
plug these numbers into a calculator, what falls out" layer that the
reasoning engine can call to avoid hallucinating arithmetic.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Optional


@dataclass
class ScenarioResult:
    expected:    float
    optimistic:  float
    pessimistic: float
    assumptions: list[str]

    def to_dict(self) -> dict:
        return {
            "expected":    self.expected,
            "optimistic":  self.optimistic,
            "pessimistic": self.pessimistic,
            "assumptions": self.assumptions,
        }


def _bracket(value: float, change: float, uncertainty: float) -> ScenarioResult:
    # "optimistic" = smaller magnitude of the change applied; "pessimistic"
    # = larger. This convention is symmetric for positive and negative
    # changes — the caller labels what "smaller" means in their domain.
    expected    = value + change
    optimistic  = value + change * (1.0 - uncertainty)
    pessimistic = value + change * (1.0 + uncertainty)
    return ScenarioResult(
        expected=expected,
        optimistic=optimistic,
        pessimistic=pessimistic,
        assumptions=[
            f"uncertainty ±{uncertainty * 100:.0f}% applied to the change",
        ],
    )


# ── Scenario shapes ─────────────────────────────────────────────────────

def percent_change(baseline: float, pct: float,
                   uncertainty: float = 0.2) -> ScenarioResult:
    """E.g. 'what if expenses increase 15%?' — baseline 2000, pct 0.15."""
    change = baseline * pct
    out = _bracket(baseline, change, uncertainty)
    out.assumptions.insert(
        0, f"baseline {baseline:g}, change {pct * 100:+.1f}%",
    )
    return out


def shift_deadline(deadline: _dt.date | _dt.datetime,
                   delta_days: int,
                   uncertainty_days: int = 0) -> dict:
    """E.g. 'what if the deadline moves a week?' — returns new expected /
    optimistic / pessimistic dates."""
    if isinstance(deadline, _dt.date) and not isinstance(deadline, _dt.datetime):
        d = deadline
    else:
        d = deadline.date()
    expected = d + _dt.timedelta(days=delta_days)
    optimistic = expected - _dt.timedelta(days=uncertainty_days)
    pessimistic = expected + _dt.timedelta(days=uncertainty_days)
    return {
        "expected":    expected.isoformat(),
        "optimistic":  optimistic.isoformat(),
        "pessimistic": pessimistic.isoformat(),
        "assumptions": [
            f"baseline {d.isoformat()}, shift {delta_days:+d} days",
            f"uncertainty ±{uncertainty_days} days",
        ],
    }


def delay_task(remaining_hours: float, additional_hours: float,
               velocity_hours_per_day: float = 6.0,
               uncertainty: float = 0.25) -> dict:
    """E.g. 'what happens if I delay this task by another 10 hours?' —
    returns expected finish offset (days) with brackets."""
    if velocity_hours_per_day <= 0:
        raise ValueError("velocity must be positive")
    total = remaining_hours + additional_hours
    expected_days = total / velocity_hours_per_day
    bracket = _bracket(0.0, expected_days, uncertainty)
    return {
        **bracket.to_dict(),
        "assumptions": [
            f"remaining {remaining_hours:g}h + delay {additional_hours:+g}h",
            f"velocity {velocity_hours_per_day:g} h/day",
            f"uncertainty ±{uncertainty * 100:.0f}%",
        ],
    }


def demand_change(current_demand: float, factor: float,
                  unit_cost: Optional[float] = None,
                  uncertainty: float = 0.2) -> dict:
    """E.g. 'what if product demand doubles?' — factor=2.0. Optional
    unit_cost returns a cost projection."""
    expected_demand = current_demand * factor
    change = expected_demand - current_demand
    demand_bracket = _bracket(current_demand, change, uncertainty)
    out = {"demand": demand_bracket.to_dict()}
    if unit_cost is not None:
        out["cost"] = ScenarioResult(
            expected=demand_bracket.expected * unit_cost,
            optimistic=demand_bracket.optimistic * unit_cost,
            pessimistic=demand_bracket.pessimistic * unit_cost,
            assumptions=[f"unit cost {unit_cost:g}"],
        ).to_dict()
    return out


def traffic_surge(current_rps: float, multiplier: float,
                  capacity_rps: float,
                  uncertainty: float = 0.2) -> dict:
    """E.g. 'what if the server gets 2x traffic?' — returns projected RPS
    with brackets and a bool `exceeds_capacity`."""
    projected = current_rps * multiplier
    bracket = _bracket(current_rps,
                       projected - current_rps,
                       uncertainty)
    return {
        **bracket.to_dict(),
        "capacity_rps":      capacity_rps,
        "exceeds_capacity":  bracket.pessimistic > capacity_rps,
        "headroom_rps":      capacity_rps - bracket.expected,
    }
