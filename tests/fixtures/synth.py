"""Synthetic OHLCV helpers for tests. Clearly labeled — NEVER market data."""
from __future__ import annotations

import math
from typing import Sequence

from market.types import Candle, OHLCVFrame, Timeframe, TIMEFRAME_MS


def make_candle(
    open_ms: int,
    tf: Timeframe,
    *,
    o: float = 100.0,
    h: float | None = None,
    l: float | None = None,
    c: float = 100.0,
    v: float = 1.0,
    closed: bool = True,
) -> Candle:
    step = TIMEFRAME_MS[tf]
    h = h if h is not None else max(o, c) + 0.5
    l = l if l is not None else min(o, c) - 0.5
    return Candle(
        open_time_ms=open_ms,
        open=o,
        high=h,
        low=l,
        close=c,
        volume=v,
        close_time_ms=open_ms + step,
        is_closed=closed,
    )


def constant_frame(
    n: int,
    tf: Timeframe = Timeframe.M1,
    start_ms: int = 1_700_000_000_000,
    price: float = 100.0,
) -> OHLCVFrame:
    step = TIMEFRAME_MS[tf]
    candles = tuple(
        make_candle(start_ms + i * step, tf, o=price, c=price, h=price + 0.1, l=price - 0.1)
        for i in range(n)
    )
    return OHLCVFrame(
        symbol="SYNTHUSDT",
        timeframe=tf,
        source="synth",
        retrieved_at_ms=start_ms + n * step,
        candles=candles,
    )


def trending_frame(
    n: int,
    tf: Timeframe = Timeframe.M1,
    start_ms: int = 1_700_000_000_000,
    start_price: float = 100.0,
    step_price: float = 0.5,
) -> OHLCVFrame:
    step_ms = TIMEFRAME_MS[tf]
    candles = []
    for i in range(n):
        price = start_price + i * step_price
        candles.append(
            make_candle(start_ms + i * step_ms, tf, o=price, c=price + 0.1, h=price + 0.2, l=price - 0.1)
        )
    return OHLCVFrame(
        symbol="SYNTHUSDT",
        timeframe=tf,
        source="synth",
        retrieved_at_ms=start_ms + n * step_ms,
        candles=tuple(candles),
    )


def sinusoidal_frame(
    n: int,
    tf: Timeframe = Timeframe.M1,
    start_ms: int = 1_700_000_000_000,
    base: float = 100.0,
    amplitude: float = 5.0,
    period: int = 20,
) -> OHLCVFrame:
    step_ms = TIMEFRAME_MS[tf]
    candles: list[Candle] = []
    for i in range(n):
        price = base + amplitude * math.sin(2 * math.pi * i / period)
        next_price = base + amplitude * math.sin(2 * math.pi * (i + 1) / period)
        o = price
        c = next_price
        h = max(o, c) + 0.1
        l = min(o, c) - 0.1
        candles.append(make_candle(start_ms + i * step_ms, tf, o=o, c=c, h=h, l=l, v=1.0))
    return OHLCVFrame(
        symbol="SYNTHUSDT",
        timeframe=tf,
        source="synth",
        retrieved_at_ms=start_ms + n * step_ms,
        candles=tuple(candles),
    )


def zigzag_frame(
    swing_prices: Sequence[float],
    bars_between: int = 5,
    tf: Timeframe = Timeframe.M1,
    start_ms: int = 1_700_000_000_000,
) -> OHLCVFrame:
    """Build a clean zigzag with strict local maxima/minima at every swing.

    Each pivot is placed `bars_between` bars after the previous one. The high
    at a swing-high pivot strictly exceeds every neighbour's high within the
    confirmation window, and symmetrically for lows.
    """
    step_ms = TIMEFRAME_MS[tf]
    candles: list[Candle] = []
    all_prices: list[float] = []
    for i, target in enumerate(swing_prices):
        if i == 0:
            all_prices.append(float(target))
            continue
        prev = swing_prices[i - 1]
        for j in range(1, bars_between):
            all_prices.append(prev + (target - prev) * j / bars_between)
        all_prices.append(float(target))
    for i, p in enumerate(all_prices):
        o = p - 0.05
        c = p + 0.05
        # Peaks: high above neighbours. For a non-swing index, keep a smaller
        # high so the swing wins by a clear margin.
        h = max(o, c) + 0.1
        l = min(o, c) - 0.1
        candles.append(make_candle(start_ms + i * step_ms, tf, o=o, h=h, l=l, c=c))
    return OHLCVFrame(
        symbol="SYNTHUSDT",
        timeframe=tf,
        source="synth",
        retrieved_at_ms=start_ms + len(all_prices) * step_ms,
        candles=tuple(candles),
    )


def mutate_candles(
    candles: Sequence[Candle],
    insert: list[Candle] | None = None,
    drop_indexes: list[int] | None = None,
    duplicate_indexes: list[int] | None = None,
) -> tuple[Candle, ...]:
    out = list(candles)
    if drop_indexes:
        out = [c for i, c in enumerate(out) if i not in set(drop_indexes)]
    if duplicate_indexes:
        for i in duplicate_indexes:
            out.append(out[i])
    if insert:
        out.extend(insert)
    return tuple(out)
