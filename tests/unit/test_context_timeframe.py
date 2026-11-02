"""Context values must name the timeframe they were observed on."""
from __future__ import annotations

from market.analysis.context import btc_trend_snapshot, correlation_with_btc
from market.types import Timeframe
from tests.fixtures.synth import trending_frame


class _MockProvider:
    name = "mock"

    def __init__(self, frames):
        self._frames = frames

    def ohlcv(self, symbol, timeframe, limit, end_ts_ms=None):
        return self._frames[symbol]

    def ticker(self, symbol):
        return {}

    def is_symbol_supported(self, symbol):
        return True


def test_btc_trend_names_timeframe() -> None:
    p = _MockProvider({"BTC": trending_frame(200, Timeframe.D1, step_price=1.0)})
    res = btc_trend_snapshot(p, Timeframe.D1)
    assert res.available
    assert res.value["timeframe"] == "1d"
    assert "last_candle_time_ms" in res.value


def test_btc_trend_timeframe_h4() -> None:
    p = _MockProvider({"BTC": trending_frame(200, Timeframe.H4, step_price=0.5)})
    res = btc_trend_snapshot(p, Timeframe.H4)
    assert res.value["timeframe"] == "4h"


def test_correlation_names_timeframe() -> None:
    p = _MockProvider({
        "BTC": trending_frame(150, Timeframe.D1, step_price=1.0),
        "ETH": trending_frame(150, Timeframe.D1, step_price=1.0),
    })
    res = correlation_with_btc(p, "ETH", Timeframe.D1, bars=60)
    assert res.available
    assert res.value["timeframe"] == "1d"
