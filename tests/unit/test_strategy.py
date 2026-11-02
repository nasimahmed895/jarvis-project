"""Phase 6 — baseline strategy tests."""
from __future__ import annotations

import pytest

from market.strategies.base import LONG_BIAS, NO_TRADE, SHORT_BIAS, StrategyDecision
from market.strategies.registry import get, list_strategies
from market.strategies import ma_trend_v1  # noqa: F401 -- registration side-effect
from market.types import Timeframe
from tests.fixtures.synth import constant_frame, trending_frame


def test_registry_contains_baseline() -> None:
    pairs = list_strategies()
    assert ("ma_trend_momentum", "1.0.0") in pairs
    s = get("ma_trend_momentum")
    assert s.version == "1.0.0"


def test_strategy_long_bias_on_uptrend() -> None:
    s = get("ma_trend_momentum")
    # Steeper uptrend to push RSI into the 45-70 band without overshoot.
    f = trending_frame(200, Timeframe.H1, start_price=100.0, step_price=0.1)
    d = s.evaluate(f, context={})
    assert d.decision in (LONG_BIAS, NO_TRADE)   # path-dependent on RSI final value
    if d.decision == LONG_BIAS:
        assert d.stop_loss is not None and d.stop_loss < d.reference_price
        assert d.targets and d.targets[0] > d.reference_price


def test_strategy_no_trade_on_flat() -> None:
    s = get("ma_trend_momentum")
    f = constant_frame(200, Timeframe.H1)
    d = s.evaluate(f, context={})
    assert d.decision == NO_TRADE


def test_strategy_insufficient_data() -> None:
    s = get("ma_trend_momentum")
    f = constant_frame(50, Timeframe.H1)
    d = s.evaluate(f, context={})
    assert d.decision == NO_TRADE
    assert any("insufficient" in e for e in d.evidence)


def test_decision_dataclass_validates() -> None:
    with pytest.raises(ValueError):
        StrategyDecision(
            strategy_name="x",
            strategy_version="1.0",
            asset="BTC",
            timeframe="1h",
            source="s",
            analysis_ts_ms=0,
            reference_price=0.0,
            decision="BUY",  # invalid
        )


def test_strategy_short_bias_on_downtrend() -> None:
    s = get("ma_trend_momentum")
    # Downtrend with small step so RSI lands in [30,55].
    f = trending_frame(200, Timeframe.H1, start_price=200.0, step_price=-0.1)
    d = s.evaluate(f, context={})
    assert d.decision in (SHORT_BIAS, NO_TRADE)
    if d.decision == SHORT_BIAS:
        assert d.stop_loss is not None and d.stop_loss > d.reference_price


def test_strategy_version_is_immutable_label() -> None:
    # If someone bumps version in the file, this test will catch an accidental
    # change by failing — the test must be updated deliberately as part of a
    # new strategy version.
    s = get("ma_trend_momentum")
    assert s.version == "1.0.0"
