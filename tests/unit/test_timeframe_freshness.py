"""Timeframe-aware freshness regression tests.

The validator receives a per-timeframe threshold from the pipeline instead
of a single global one. A 1-day candle that is 2 hours past its close must
pass; a 1-minute candle that is 5 minutes past its close must fail.
"""
from __future__ import annotations

import pytest

from market.data.validator import validate_frame
from market.exceptions import StaleData
from market.pipeline import timeframe_staleness_threshold
from market.types import Timeframe, TIMEFRAME_MS
from tests.fixtures.synth import constant_frame


def test_threshold_scales_with_timeframe() -> None:
    assert timeframe_staleness_threshold(Timeframe.M1, 120) == 120   # floor wins
    assert timeframe_staleness_threshold(Timeframe.H1, 120) == 5400  # 1.5 * 3600
    assert timeframe_staleness_threshold(Timeframe.H4, 120) == int(1.5 * 4 * 3600)
    assert timeframe_staleness_threshold(Timeframe.D1, 120) == int(1.5 * 86400)


def test_daily_candle_two_hours_past_close_is_fresh() -> None:
    f = constant_frame(5, Timeframe.D1)
    last_close = f.candles[-1].close_time_ms
    now_ms = last_close + 2 * 3600 * 1000  # 2h since close
    threshold = timeframe_staleness_threshold(Timeframe.D1, 120)
    cleaned, q = validate_frame(f, now_ms, threshold)
    assert cleaned is not None
    assert q.stale_age_seconds == pytest.approx(2 * 3600, rel=1e-6)


def test_one_minute_candle_five_minutes_past_close_is_stale() -> None:
    f = constant_frame(5, Timeframe.M1)
    last_close = f.candles[-1].close_time_ms
    now_ms = last_close + 5 * 60 * 1000  # 5 min since close
    threshold = timeframe_staleness_threshold(Timeframe.M1, 120)  # 120s floor
    with pytest.raises(StaleData):
        validate_frame(f, now_ms, threshold)


def test_hour_candle_one_hour_past_close_is_fresh() -> None:
    f = constant_frame(5, Timeframe.H1)
    last_close = f.candles[-1].close_time_ms
    now_ms = last_close + 3600 * 1000      # exactly one hour past close
    threshold = timeframe_staleness_threshold(Timeframe.H1, 120)
    cleaned, _ = validate_frame(f, now_ms, threshold)
    assert cleaned is not None


def test_hour_candle_two_hours_past_close_is_stale() -> None:
    f = constant_frame(5, Timeframe.H1)
    last_close = f.candles[-1].close_time_ms
    now_ms = last_close + 2 * 3600 * 1000 + 1  # 2h+1ms past close (threshold = 1.5h)
    threshold = timeframe_staleness_threshold(Timeframe.H1, 120)
    with pytest.raises(StaleData):
        validate_frame(f, now_ms, threshold)
