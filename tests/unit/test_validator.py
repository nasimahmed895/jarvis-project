"""Phase 3 — validator unit tests."""
from __future__ import annotations

import pytest

from market.data.validator import closed_candles_only, validate_frame
from market.exceptions import StaleData, ValidationError
from market.types import OHLCVFrame, Timeframe, TIMEFRAME_MS
from tests.fixtures.synth import constant_frame, make_candle, mutate_candles


def test_valid_frame_passes() -> None:
    f = constant_frame(50, Timeframe.M1)
    now_ms = f.candles[-1].close_time_ms + 10_000
    cleaned, q = validate_frame(f, now_ms, staleness_seconds=60)
    assert q.missing_candles == 0
    assert q.duplicate_timestamps == 0
    assert q.out_of_order == 0
    assert q.completeness == 1.0
    assert len(cleaned) == 50


def test_duplicates_removed() -> None:
    f = constant_frame(10, Timeframe.M1)
    dup = mutate_candles(f.candles, duplicate_indexes=[3, 5])
    f2 = OHLCVFrame(
        symbol=f.symbol,
        timeframe=f.timeframe,
        source=f.source,
        retrieved_at_ms=f.retrieved_at_ms,
        candles=dup,
    )
    now_ms = f.candles[-1].close_time_ms + 10_000
    cleaned, q = validate_frame(f2, now_ms, staleness_seconds=60)
    assert q.duplicate_timestamps == 2
    assert len(cleaned) == 10


def test_gaps_counted() -> None:
    f = constant_frame(10, Timeframe.M1)
    dropped = mutate_candles(f.candles, drop_indexes=[4, 7])
    f2 = OHLCVFrame(
        symbol=f.symbol,
        timeframe=f.timeframe,
        source=f.source,
        retrieved_at_ms=f.retrieved_at_ms,
        candles=dropped,
    )
    now_ms = f.candles[-1].close_time_ms + 10_000
    cleaned, q = validate_frame(f2, now_ms, staleness_seconds=60)
    assert q.missing_candles == 2


def test_out_of_order_counted() -> None:
    f = constant_frame(6, Timeframe.M1)
    reordered = (f.candles[0], f.candles[2], f.candles[1], f.candles[3], f.candles[4], f.candles[5])
    f2 = OHLCVFrame(
        symbol=f.symbol,
        timeframe=f.timeframe,
        source=f.source,
        retrieved_at_ms=f.retrieved_at_ms,
        candles=reordered,
    )
    now_ms = f.candles[-1].close_time_ms + 10_000
    _, q = validate_frame(f2, now_ms, staleness_seconds=60)
    assert q.out_of_order == 1


def test_stale_raises() -> None:
    f = constant_frame(3, Timeframe.M1)
    now_ms = f.candles[-1].close_time_ms + 10 * 60_000  # 10 min after close
    with pytest.raises(StaleData):
        validate_frame(f, now_ms, staleness_seconds=120)


def test_insufficient_candles() -> None:
    f = constant_frame(1, Timeframe.M1)
    now_ms = f.candles[-1].close_time_ms + 1_000
    with pytest.raises(ValidationError, match="insufficient"):
        validate_frame(f, now_ms, staleness_seconds=60, min_candles=10)


def test_closed_candles_only_drops_open() -> None:
    f = constant_frame(5, Timeframe.M1)
    open_candle = make_candle(f.candles[-1].close_time_ms, Timeframe.M1, closed=False)
    f2 = OHLCVFrame(
        symbol=f.symbol,
        timeframe=f.timeframe,
        source=f.source,
        retrieved_at_ms=f.retrieved_at_ms,
        candles=f.candles + (open_candle,),
    )
    out = closed_candles_only(f2)
    assert len(out) == 5
    assert all(c.is_closed for c in out.candles)


def test_no_closed_candles_raises() -> None:
    f = constant_frame(3, Timeframe.M1)
    all_open = tuple(
        make_candle(c.open_time_ms, Timeframe.M1, closed=False) for c in f.candles
    )
    f2 = OHLCVFrame(
        symbol=f.symbol,
        timeframe=f.timeframe,
        source=f.source,
        retrieved_at_ms=f.retrieved_at_ms,
        candles=all_open,
    )
    with pytest.raises(ValidationError, match="no closed"):
        validate_frame(f2, now_ms=f2.candles[-1].close_time_ms + 1000, staleness_seconds=60)
