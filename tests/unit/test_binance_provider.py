"""Phase 3 — Binance public provider tests with mocked HTTP.

The real network is NEVER touched here. A `MockSession` replaces
`requests.Session`. A separate network-marked test (opt-in, skipped by
default) hits the live public endpoint — see `tests/integration`.
"""
from __future__ import annotations

import json
import time
from typing import Any

import pytest

from market.exceptions import ProviderError, RateLimitError
from market.providers.binance_public import BinancePublicProvider
from market.types import Timeframe


class _Resp:
    def __init__(self, status_code: int, body: Any):
        self.status_code = status_code
        self._body = body
        self.text = json.dumps(body) if not isinstance(body, str) else body

    def json(self):
        if isinstance(self._body, str):
            raise ValueError("not json")
        return self._body


class _MockSession:
    def __init__(self, responses: list[_Resp]):
        self._responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append((url, params or {}))
        if not self._responses:
            raise AssertionError("MockSession exhausted")
        return self._responses.pop(0)


def _klines_row(open_ms: int, close_ms: int, o=100.0, h=101.0, l=99.0, c=100.5, v=1.0):
    return [open_ms, str(o), str(h), str(l), str(c), str(v), close_ms, "0", 0, "0", "0", "0"]


def test_is_symbol_supported_cache() -> None:
    body = {
        "symbols": [
            {"symbol": "BTCUSDT", "status": "TRADING", "isSpotTradingAllowed": True},
            {"symbol": "ETHUSDT", "status": "TRADING", "isSpotTradingAllowed": True},
            {"symbol": "FOOUSDT", "status": "HALT", "isSpotTradingAllowed": False},
        ]
    }
    sess = _MockSession([_Resp(200, body)])
    p = BinancePublicProvider(session=sess)
    assert p.is_symbol_supported("BTC") is True
    assert p.is_symbol_supported("ETH") is True
    assert p.is_symbol_supported("FOO") is False
    # Second lookup must not hit the network — only one request so far.
    assert len(sess.calls) == 1


def test_ticker_parses_price_and_24h() -> None:
    sess = _MockSession([
        _Resp(200, {"price": "26500.50"}),
        _Resp(200, {
            "priceChangePercent": "1.23",
            "highPrice": "27000", "lowPrice": "26000",
            "volume": "1234.5", "quoteVolume": "32768000",
        }),
    ])
    p = BinancePublicProvider(session=sess)
    t = p.ticker("BTC")
    assert t["symbol"] == "BTCUSDT"
    assert t["price"] == 26500.50
    assert abs(t["price_change_pct_24h"] - 1.23) < 1e-9
    assert t["source"] == "binance_public"


def test_ohlcv_parses_klines() -> None:
    now_ms = int(time.time() * 1000)
    rows = [
        _klines_row(now_ms - 60_000 * i, now_ms - 60_000 * (i - 1))
        for i in range(5, 0, -1)
    ]
    sess = _MockSession([_Resp(200, rows)])
    p = BinancePublicProvider(session=sess)
    f = p.ohlcv("BTC", Timeframe.M1, limit=5)
    assert len(f) == 5
    assert f.symbol == "BTCUSDT"
    assert f.timeframe is Timeframe.M1
    assert f.source == "binance_public"


def test_ohlcv_rejects_bad_limit() -> None:
    p = BinancePublicProvider(session=_MockSession([]))
    with pytest.raises(ProviderError, match="limit"):
        p.ohlcv("BTC", Timeframe.M1, limit=0)
    with pytest.raises(ProviderError, match="limit"):
        p.ohlcv("BTC", Timeframe.M1, limit=1001)


def test_rate_limited_raises() -> None:
    sess = _MockSession([_Resp(429, {"msg": "too many"})])
    p = BinancePublicProvider(session=sess)
    with pytest.raises(RateLimitError):
        p.ticker("BTC")


def test_5xx_retries_then_fails() -> None:
    sess = _MockSession([_Resp(500, {}), _Resp(503, {}), _Resp(500, {})])
    p = BinancePublicProvider(session=sess)
    with pytest.raises(Exception):
        p.ticker("BTC")
    assert len(sess.calls) == 3


def test_quote_currency_configurable() -> None:
    body = {"symbols": [
        {"symbol": "BTCFDUSD", "status": "TRADING", "isSpotTradingAllowed": True},
    ]}
    sess = _MockSession([_Resp(200, body)])
    p = BinancePublicProvider(session=sess, quote_currency="FDUSD")
    assert p.is_symbol_supported("BTC") is True
