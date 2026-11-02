"""Phase 7 — risk engine tests."""
from __future__ import annotations

import pytest

from market.exceptions import RiskRejection
from market.risk.gates import AccountState, RiskParams, check_trade, require
from market.risk.sizer import size_position


def _params(**overrides):
    base = dict(
        risk_per_trade=0.01,
        max_concurrent_positions=3,
        max_daily_loss_frac=0.03,
        max_drawdown_frac=0.15,
        min_rr=1.5,
        max_spread_bps=25.0,
        max_data_age_seconds=120.0,
        fee_rate=0.001,
        slippage_bps=5.0,
    )
    base.update(overrides)
    return RiskParams(**base)


def _acct(**overrides):
    base = dict(
        equity_usd=10_000.0,
        available_cash_usd=10_000.0,
        open_positions=0,
        realised_pnl_today_usd=0.0,
        peak_equity_usd=10_000.0,
    )
    base.update(overrides)
    return AccountState(**base)


def test_sizer_simple() -> None:
    r = size_position(
        equity_usd=10_000,
        risk_fraction=0.01,
        entry_price=100.0,
        stop_price=95.0,
        available_cash_usd=10_000,
    )
    assert r.qty > 0
    assert r.risk_budget_usd == pytest.approx(100.0)


def test_sizer_zero_distance_rejects() -> None:
    r = size_position(
        equity_usd=10_000,
        risk_fraction=0.01,
        entry_price=100.0,
        stop_price=100.0,
        available_cash_usd=10_000,
    )
    assert r.qty == 0.0
    assert "entry equals stop" in " ".join(r.reasons)


def test_sizer_insufficient_cash() -> None:
    r = size_position(
        equity_usd=100_000,
        risk_fraction=0.1,
        entry_price=100.0,
        stop_price=99.0,
        available_cash_usd=500,
    )
    assert r.qty == 0.0


def test_sizer_qty_step_floor() -> None:
    r = size_position(
        equity_usd=1_000,
        risk_fraction=0.1,
        entry_price=100.0,
        stop_price=90.0,
        available_cash_usd=10_000,
        qty_step=0.1,
    )
    # 10% of 1000 = 100 risk / 10 dist = 10 qty (clean step).
    assert r.qty == pytest.approx(10.0)


def test_gate_accepts_valid_long() -> None:
    d = check_trade(
        side="LONG",
        entry_price=100.0,
        stop_price=98.0,
        target_price=103.5,  # RR = 3.5/2 = 1.75
        spread_bps=5.0,
        data_age_seconds=10.0,
        account=_acct(),
        params=_params(),
    )
    assert d.accepted is True
    assert d.code == "OK"


def test_gate_rejects_bad_side() -> None:
    d = check_trade(
        side="LONG",
        entry_price=100.0,
        stop_price=101.0,
        target_price=103.0,
        spread_bps=5,
        data_age_seconds=10,
        account=_acct(),
        params=_params(),
    )
    assert d.accepted is False
    assert d.code == "BAD_SIDE"


def test_gate_rejects_bad_rr() -> None:
    d = check_trade(
        side="LONG",
        entry_price=100.0,
        stop_price=99.0,
        target_price=100.5,  # RR = 0.5
        spread_bps=5,
        data_age_seconds=10,
        account=_acct(),
        params=_params(),
    )
    assert d.accepted is False
    assert d.code == "BAD_RR"


def test_gate_rejects_stale_data() -> None:
    d = check_trade(
        side="LONG",
        entry_price=100.0,
        stop_price=98.0,
        target_price=103.0,
        spread_bps=5,
        data_age_seconds=500,
        account=_acct(),
        params=_params(),
    )
    assert d.accepted is False
    assert d.code == "STALE"


def test_gate_rejects_wide_spread() -> None:
    d = check_trade(
        side="LONG",
        entry_price=100.0,
        stop_price=98.0,
        target_price=103.0,
        spread_bps=100,
        data_age_seconds=10,
        account=_acct(),
        params=_params(),
    )
    assert d.accepted is False
    assert d.code == "SPREAD"


def test_gate_rejects_daily_loss() -> None:
    d = check_trade(
        side="LONG",
        entry_price=100.0,
        stop_price=98.0,
        target_price=103.0,
        spread_bps=5,
        data_age_seconds=10,
        account=_acct(realised_pnl_today_usd=-400.0),  # -4% > 3% limit
        params=_params(),
    )
    assert d.accepted is False
    assert d.code == "DAILY_LOSS"


def test_gate_rejects_drawdown() -> None:
    d = check_trade(
        side="LONG",
        entry_price=100.0,
        stop_price=98.0,
        target_price=103.0,
        spread_bps=5,
        data_age_seconds=10,
        account=_acct(equity_usd=8_000, peak_equity_usd=10_000),  # 20% dd > 15%
        params=_params(),
    )
    assert d.accepted is False
    assert d.code == "DRAWDOWN"


def test_gate_rejects_max_positions() -> None:
    d = check_trade(
        side="LONG",
        entry_price=100.0,
        stop_price=98.0,
        target_price=103.0,
        spread_bps=5,
        data_age_seconds=10,
        account=_acct(open_positions=3),
        params=_params(),
    )
    assert d.accepted is False
    assert d.code == "MAX_POS"


def test_require_raises_on_rejection() -> None:
    d = check_trade(
        side="LONG",
        entry_price=100.0,
        stop_price=101.0,
        target_price=103.0,
        spread_bps=5,
        data_age_seconds=10,
        account=_acct(),
        params=_params(),
    )
    with pytest.raises(RiskRejection) as exc:
        require(d)
    assert exc.value.code == "BAD_SIDE"


def test_require_returns_size_on_accept() -> None:
    d = check_trade(
        side="LONG",
        entry_price=100.0,
        stop_price=98.0,
        target_price=104.0,
        spread_bps=5,
        data_age_seconds=10,
        account=_acct(),
        params=_params(),
    )
    size = require(d)
    assert size.qty > 0
