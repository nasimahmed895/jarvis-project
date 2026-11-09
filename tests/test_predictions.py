"""Tests for core/predictions.py — the prediction ledger."""
from __future__ import annotations

from pathlib import Path

import pytest

from core import predictions


@pytest.fixture(autouse=True)
def _temp_db(tmp_path: Path):
    db = tmp_path / "predictions.sqlite"
    predictions._DB = None
    predictions._DB_PATH = None
    predictions.connect(db)
    yield
    predictions._DB = None
    predictions._DB_PATH = None


def test_record_and_get():
    pid = predictions.record(
        category="time", subject="Project X ships",
        kind="numeric", point=1700000000.0,
        confidence=0.6, horizon_s=86400,
        evidence={"basis": "velocity"}, assumptions="steady state")
    row = predictions.get(pid)
    assert row is not None
    assert row.category == "time"
    assert row.status == "open"
    assert row.evidence == {"basis": "velocity"}


def test_resolve_numeric_computes_error():
    pid = predictions.record(category="finance", subject="Nov spend",
                             point=2000.0)
    err = predictions.resolve(pid, actual=2150.0)
    assert err == pytest.approx(150.0)
    row = predictions.get(pid)
    assert row.status == "resolved"
    assert row.actual == 2150.0


def test_resolve_probability_requires_binary_actual():
    pid = predictions.record(category="risk",
                             subject="Deadline missed",
                             kind="probability", point=0.3,
                             confidence=0.5)
    with pytest.raises(ValueError):
        predictions.resolve(pid, actual=0.5)
    err = predictions.resolve(pid, actual=1.0)
    assert err == pytest.approx(0.7)


def test_metrics_mae_and_rmse():
    for actual, point in [(10.0, 8.0), (20.0, 25.0), (30.0, 32.0)]:
        pid = predictions.record(category="demand", subject="units",
                                 point=point)
        predictions.resolve(pid, actual=actual)
    m = predictions.metrics(category="demand", kind="numeric")
    assert m["numeric_n"] == 3
    assert m["mae"] == pytest.approx((2 + 5 + 2) / 3)
    # RMSE sqrt((4 + 25 + 4)/3) ≈ 3.11
    assert m["rmse"] == pytest.approx(((4 + 25 + 4) / 3) ** 0.5)


def test_metrics_brier_and_calibration():
    for prob, actual in [(0.1, 0), (0.2, 0), (0.8, 1), (0.9, 1), (0.6, 0)]:
        pid = predictions.record(category="risk", subject="x",
                                 kind="probability", point=prob)
        predictions.resolve(pid, actual=float(actual))
    m = predictions.metrics(category="risk", kind="probability")
    assert m["probability_n"] == 5
    # Brier = mean squared error = (0.01+0.04+0.04+0.01+0.36)/5 = 0.092
    assert m["brier"] == pytest.approx((0.01 + 0.04 + 0.04 + 0.01 + 0.36) / 5)
    # Calibration buckets include at least one row with mean_actual.
    assert any(b["n"] > 0 for b in m["calibration"])


def test_cancel_and_resolve_guard():
    pid = predictions.record(category="x", subject="y", point=1.0)
    predictions.cancel(pid)
    with pytest.raises(ValueError):
        predictions.resolve(pid, actual=1.0)


def test_expire_overdue_cancels_stale_open():
    # Horizon in the past, created_at + horizon + 7d < now
    pid = predictions.record(category="x", subject="y", point=1.0,
                             horizon_s=60)
    # Force created_at back
    predictions._db().execute(
        "UPDATE predictions SET created_at=? WHERE id=?",
        (0, pid))
    n = predictions.expire_overdue()
    assert n >= 1
    assert predictions.get(pid).status == "cancelled"
