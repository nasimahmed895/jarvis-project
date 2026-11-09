"""Tests for core/scenarios.py — pure what-if arithmetic."""
from __future__ import annotations

import datetime as _dt

import pytest

from core import scenarios


def test_percent_change_positive():
    out = scenarios.percent_change(1000.0, 0.15, uncertainty=0.2)
    assert out.expected == pytest.approx(1150.0)
    # +15% * (1 - 0.2) = +12% → 1120
    assert out.optimistic == pytest.approx(1120.0)
    # +15% * (1 + 0.2) = +18% → 1180
    assert out.pessimistic == pytest.approx(1180.0)
    assert out.pessimistic > out.expected > out.optimistic


def test_percent_change_negative_change():
    # Change is a reduction; optimistic should be LESS reduction (bigger
    # ending value) and pessimistic MORE reduction (smaller).
    out = scenarios.percent_change(1000.0, -0.10, uncertainty=0.2)
    assert out.expected == pytest.approx(900.0)
    # -10% * (1+0.2) = -8% → 920
    assert out.optimistic == pytest.approx(920.0)
    # -10% * (1-0.2) = -12% → 880
    assert out.pessimistic == pytest.approx(880.0)


def test_shift_deadline():
    d = _dt.date(2026, 11, 20)
    out = scenarios.shift_deadline(d, delta_days=7, uncertainty_days=2)
    assert out["expected"] == "2026-11-27"
    assert out["optimistic"] == "2026-11-25"
    assert out["pessimistic"] == "2026-11-29"


def test_delay_task():
    out = scenarios.delay_task(remaining_hours=20, additional_hours=10,
                               velocity_hours_per_day=5, uncertainty=0.25)
    assert out["expected"] == pytest.approx(6.0)  # 30h / 5 = 6 days
    assert out["optimistic"] < out["expected"] < out["pessimistic"]


def test_demand_change_with_cost():
    out = scenarios.demand_change(current_demand=100, factor=2.0,
                                  unit_cost=3.0, uncertainty=0.2)
    assert out["demand"]["expected"] == pytest.approx(200.0)
    assert out["cost"]["expected"] == pytest.approx(600.0)


def test_traffic_surge_flags_overcapacity():
    over = scenarios.traffic_surge(current_rps=100, multiplier=3,
                                   capacity_rps=200, uncertainty=0.1)
    assert over["exceeds_capacity"] is True
    under = scenarios.traffic_surge(current_rps=100, multiplier=1.5,
                                    capacity_rps=500, uncertainty=0.1)
    assert under["exceeds_capacity"] is False
