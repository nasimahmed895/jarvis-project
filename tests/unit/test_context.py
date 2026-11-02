"""Phase 5 — context analysis tests (mocked provider)."""
from __future__ import annotations

import pytest

from market.analysis.context import (
    btc_trend_snapshot,
    correlation_with_btc,
    news_sentiment,
)
from market.types import Timeframe
from tests.fixtures.synth import trending_frame


class _MockProvider:
    name = "mock"

    def __init__(self, frames: dict[str, object], raise_for: set[str] | None = None):
        self._frames = frames
        self._raise = raise_for or set()

    def ohlcv(self, symbol, timeframe, limit, end_ts_ms=None):
        if symbol in self._raise:
            raise RuntimeError("boom")
        return self._frames[symbol]

    def ticker(self, symbol):
        return {}

    def is_symbol_supported(self, symbol):
        return symbol in self._frames


def test_btc_trend_up() -> None:
    p = _MockProvider({"BTC": trending_frame(200, Timeframe.D1, step_price=1.0)})
    res = btc_trend_snapshot(p, Timeframe.D1)
    assert res.available is True
    assert res.value["direction"] == "up"


def test_btc_trend_down() -> None:
    p = _MockProvider({"BTC": trending_frame(200, Timeframe.D1, start_price=300.0, step_price=-1.0)})
    res = btc_trend_snapshot(p, Timeframe.D1)
    assert res.available is True
    assert res.value["direction"] == "down"


def test_btc_trend_provider_error_returns_unavailable() -> None:
    p = _MockProvider({}, raise_for={"BTC"})
    res = btc_trend_snapshot(p, Timeframe.D1)
    assert res.available is False
    assert "provider error" in res.reason


def test_btc_trend_insufficient_data() -> None:
    p = _MockProvider({"BTC": trending_frame(20, Timeframe.D1)})
    res = btc_trend_snapshot(p, Timeframe.D1)
    assert res.available is False


def test_correlation_available() -> None:
    frames = {
        "BTC": trending_frame(150, Timeframe.D1, step_price=1.0),
        "ETH": trending_frame(150, Timeframe.D1, step_price=1.0),
    }
    res = correlation_with_btc(_MockProvider(frames), "ETH", Timeframe.D1, bars=60)
    assert res.available is True
    assert "pearson_r" in res.value


def test_correlation_handles_error() -> None:
    p = _MockProvider({}, raise_for={"BTC"})
    res = correlation_with_btc(p, "ETH", Timeframe.D1, bars=60)
    assert res.available is False


def test_news_sentiment_unavailable_by_design() -> None:
    res = news_sentiment("BTC")
    assert res.available is False
    assert "news module not implemented" in res.reason
