"""Phase 8 — backtest engine + metrics tests."""
from __future__ import annotations

from market.backtest.engine import BacktestConfig, run_backtest
from market.backtest.metrics import compute
from market.strategies.registry import get
from market.strategies import ma_trend_v1  # noqa: F401
from market.types import Timeframe
from tests.fixtures.synth import constant_frame, trending_frame


def test_backtest_runs_on_uptrend() -> None:
    s = get("ma_trend_momentum")
    f = trending_frame(500, Timeframe.H1, start_price=100.0, step_price=0.1)
    cfg = BacktestConfig()
    r = run_backtest(f, s, cfg)
    assert r.bars > 0
    assert r.strategy_version == "1.0.0"
    assert len(r.equity_curve) == r.bars


def test_backtest_no_trade_on_flat() -> None:
    s = get("ma_trend_momentum")
    f = constant_frame(300, Timeframe.H1)
    cfg = BacktestConfig()
    r = run_backtest(f, s, cfg)
    assert len(r.trades) == 0
    assert r.final_equity_usd == cfg.initial_balance_usd


def test_metrics_shape() -> None:
    s = get("ma_trend_momentum")
    f = trending_frame(400, Timeframe.H1, start_price=100.0, step_price=0.1)
    r = run_backtest(f, s, BacktestConfig())
    m = compute(r)
    assert m.num_trades == len(r.trades)
    assert m.max_drawdown >= 0.0
    assert isinstance(m.assumptions, tuple)
    assert any("fees" in a for a in m.assumptions)


def test_metrics_no_trade_safe() -> None:
    s = get("ma_trend_momentum")
    f = constant_frame(300, Timeframe.H1)
    r = run_backtest(f, s, BacktestConfig())
    m = compute(r)
    assert m.num_trades == 0
    assert m.win_rate is None
    assert m.profit_factor is None
    assert m.expectancy_usd is None


def test_reproducible() -> None:
    s = get("ma_trend_momentum")
    f = trending_frame(400, Timeframe.H1, start_price=100.0, step_price=0.1)
    cfg = BacktestConfig()
    r1 = run_backtest(f, s, cfg)
    r2 = run_backtest(f, s, cfg)
    assert r1.final_equity_usd == r2.final_equity_usd
    assert [t.net_pnl_usd for t in r1.trades] == [t.net_pnl_usd for t in r2.trades]


def test_fees_charged() -> None:
    s = get("ma_trend_momentum")
    f = trending_frame(400, Timeframe.H1, start_price=100.0, step_price=0.1)
    r_fees = run_backtest(f, s, BacktestConfig(fee_rate=0.001))
    r_nofees = run_backtest(f, s, BacktestConfig(fee_rate=0.0))
    if r_fees.trades and r_nofees.trades:
        # If any trades fired, no-fee run must be >= fees run.
        assert r_nofees.final_equity_usd >= r_fees.final_equity_usd
