"""Phase 2 — Candle / Timeframe / OHLCVFrame unit tests."""
from __future__ import annotations

import pytest

from market.types import Candle, OHLCVFrame, Timeframe, TIMEFRAME_MS


def _mk(open_, high, low, close, *, open_ms=1_700_000_000_000, step_ms=60_000, vol=1.0, closed=True):
    return Candle(
        open_time_ms=open_ms,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=vol,
        close_time_ms=open_ms + step_ms,
        is_closed=closed,
    )


def test_candle_valid() -> None:
    c = _mk(100.0, 105.0, 95.0, 102.0)
    assert c.open == 100.0
    assert c.high == 105.0


def test_candle_rejects_bad_high() -> None:
    with pytest.raises(ValueError, match="high"):
        _mk(100.0, 90.0, 95.0, 102.0)


def test_candle_rejects_bad_low() -> None:
    # low=103 is greater than open=100, so the low check must trip even though
    # high=200 is comfortably above every other component.
    with pytest.raises(ValueError, match="low"):
        _mk(100.0, 200.0, 103.0, 102.0)


def test_candle_rejects_negative_volume() -> None:
    with pytest.raises(ValueError, match="volume"):
        _mk(100.0, 105.0, 95.0, 102.0, vol=-1)


def test_candle_rejects_bad_time_order() -> None:
    with pytest.raises(ValueError, match="close_time_ms"):
        Candle(1000, 1, 2, 1, 1.5, 1.0, 1000, True)


def test_timeframe_from_str() -> None:
    assert Timeframe.from_str("1m") is Timeframe.M1
    assert Timeframe.from_str(" 1D ") is Timeframe.D1
    with pytest.raises(ValueError):
        Timeframe.from_str("2m")


def test_timeframe_ms_table_complete() -> None:
    for tf in Timeframe:
        assert tf in TIMEFRAME_MS
        assert TIMEFRAME_MS[tf] > 0


def test_frame_latest_and_latest_closed() -> None:
    c1 = _mk(1, 2, 0.5, 1.5, open_ms=1_000, closed=True)
    c2 = _mk(1.5, 3, 1, 2.5, open_ms=61_000, closed=True)
    c3 = _mk(2.5, 4, 2, 3, open_ms=121_000, closed=False)
    f = OHLCVFrame(
        symbol="BTCUSDT",
        timeframe=Timeframe.M1,
        source="test",
        retrieved_at_ms=200_000,
        candles=(c1, c2, c3),
    )
    assert len(f) == 3
    assert f.latest is c3
    assert f.latest_closed is c2
    assert f.closes() == (1.5, 2.5, 3)


def test_empty_frame() -> None:
    f = OHLCVFrame(
        symbol="BTCUSDT",
        timeframe=Timeframe.M1,
        source="test",
        retrieved_at_ms=0,
    )
    assert len(f) == 0
    assert f.latest is None
    assert f.latest_closed is None
    assert f.closes() == ()
