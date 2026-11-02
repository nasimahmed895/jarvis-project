"""Phase 3 — SQLite OHLCV cache tests."""
from __future__ import annotations

from pathlib import Path

from market.data.cache import CandleCache
from market.types import Timeframe
from tests.fixtures.synth import constant_frame


def test_store_and_load_roundtrip(tmp_path: Path) -> None:
    cache = CandleCache(tmp_path / "c.db")
    try:
        f = constant_frame(20, Timeframe.M1)
        wrote = cache.store_frame(f)
        assert wrote == 20
        loaded = cache.load_frame(f.source, f.symbol, f.timeframe, limit=20)
        assert len(loaded) == 20
        assert [c.open_time_ms for c in loaded.candles] == [c.open_time_ms for c in f.candles]
    finally:
        cache.close()


def test_upsert_overwrites(tmp_path: Path) -> None:
    cache = CandleCache(tmp_path / "c.db")
    try:
        f = constant_frame(5, Timeframe.M1)
        cache.store_frame(f)
        # Rewrite with modified closes.
        new = constant_frame(5, Timeframe.M1, price=200.0)
        cache.store_frame(new)
        loaded = cache.load_frame(f.source, f.symbol, f.timeframe, limit=5)
        assert all(c.close == 200.0 for c in loaded.candles)
    finally:
        cache.close()


def test_latest_candle(tmp_path: Path) -> None:
    cache = CandleCache(tmp_path / "c.db")
    try:
        f = constant_frame(10, Timeframe.M1)
        cache.store_frame(f)
        latest = cache.latest_candle(f.source, f.symbol, f.timeframe)
        assert latest is not None
        assert latest.open_time_ms == f.candles[-1].open_time_ms
    finally:
        cache.close()


def test_latest_candle_none_when_empty(tmp_path: Path) -> None:
    cache = CandleCache(tmp_path / "c.db")
    try:
        assert cache.latest_candle("binance_public", "BTCUSDT", Timeframe.M1) is None
    finally:
        cache.close()


def test_load_with_end_ts(tmp_path: Path) -> None:
    cache = CandleCache(tmp_path / "c.db")
    try:
        f = constant_frame(10, Timeframe.M1)
        cache.store_frame(f)
        mid_ts = f.candles[5].open_time_ms
        out = cache.load_frame(f.source, f.symbol, f.timeframe, limit=10, end_ts_ms=mid_ts)
        assert len(out) == 6
        assert out.candles[-1].open_time_ms == mid_ts
    finally:
        cache.close()
