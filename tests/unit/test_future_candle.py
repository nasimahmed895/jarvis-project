"""Validator must drop candles whose open_time is in the future."""
from __future__ import annotations

from market.data.validator import validate_frame
from market.types import Candle, OHLCVFrame, Timeframe, TIMEFRAME_MS


def _mk(open_ms: int, closed: bool = True) -> Candle:
    return Candle(
        open_time_ms=open_ms, open=100.0, high=101.0, low=99.0,
        close=100.5, volume=1.0,
        close_time_ms=open_ms + TIMEFRAME_MS[Timeframe.M1],
        is_closed=closed,
    )


def test_future_dated_candles_are_dropped() -> None:
    now_ms = 1_700_000_000_000
    good = [_mk(now_ms - (i + 1) * 60_000) for i in range(5)]
    future = _mk(now_ms + 60 * 60_000)   # 1h in the future
    f = OHLCVFrame(
        symbol="SYNTHUSDT", timeframe=Timeframe.M1, source="mock",
        retrieved_at_ms=now_ms, candles=tuple(good + [future]),
    )
    cleaned, q = validate_frame(f, now_ms, staleness_seconds=24 * 3600)
    assert len(cleaned) == 5
    assert any("future" in w for w in q.warnings)


def test_small_clock_skew_within_grace_is_kept() -> None:
    now_ms = 1_700_000_000_000
    base = [_mk(now_ms - (i + 2) * 60_000) for i in range(5)]
    # 30-second overshoot — within the 60s grace.
    skewed = _mk(now_ms + 30_000)
    f = OHLCVFrame(
        symbol="SYNTHUSDT", timeframe=Timeframe.M1, source="mock",
        retrieved_at_ms=now_ms, candles=tuple(base + [skewed]),
    )
    cleaned, q = validate_frame(f, now_ms, staleness_seconds=24 * 3600)
    assert len(cleaned) == 6
    assert not any("future" in w for w in q.warnings)
