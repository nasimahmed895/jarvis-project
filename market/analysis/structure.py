"""Market-structure analysis.

Swing points are identified using a confirmation window `k`: index `i` is a
swing high when `high[i]` is strictly greater than every `high[j]` for
`j` in `[i-k, i-1]` and `[i+1, i+k]`. The confirmation requires `k` candles
after the swing — until those arrive, the swing is "unconfirmed".

Support/resistance zones are produced by 1-D agglomerative clustering of
confirmed swing highs (resistance) and lows (support). Zones are expressed
as a (price_low, price_high) range to acknowledge that a zone is a region,
not a precise price.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from ..types import Candle, OHLCVFrame


@dataclass(frozen=True, slots=True)
class SwingPoint:
    index: int
    time_ms: int
    price: float
    kind: str  # "high" | "low"


@dataclass(frozen=True, slots=True)
class Zone:
    kind: str  # "support" | "resistance"
    low: float
    high: float
    touches: int
    last_touch_time_ms: int

    @property
    def mid(self) -> float:
        return (self.low + self.high) / 2.0


def find_swings(frame: OHLCVFrame, k: int = 3) -> list[SwingPoint]:
    """Find confirmed swing highs and lows using a `k`-bar window on each side.

    Candles after index `len - k - 1` are considered unconfirmed and excluded.
    """
    if k < 1:
        raise ValueError("k must be at least 1")
    candles = list(frame.candles)
    n = len(candles)
    out: list[SwingPoint] = []
    for i in range(k, n - k):
        left_high = max(c.high for c in candles[i - k : i])
        right_high = max(c.high for c in candles[i + 1 : i + 1 + k])
        if candles[i].high > left_high and candles[i].high > right_high:
            out.append(
                SwingPoint(i, candles[i].open_time_ms, candles[i].high, "high")
            )
            continue
        left_low = min(c.low for c in candles[i - k : i])
        right_low = min(c.low for c in candles[i + 1 : i + 1 + k])
        if candles[i].low < left_low and candles[i].low < right_low:
            out.append(SwingPoint(i, candles[i].open_time_ms, candles[i].low, "low"))
    return out


def _cluster_prices(
    prices: list[tuple[float, int]], tolerance_pct: float
) -> list[tuple[float, float, int, int]]:
    """1-D cluster. Returns list of (low, high, count, last_time_ms)."""
    if not prices:
        return []
    sorted_prices = sorted(prices, key=lambda x: x[0])
    clusters: list[list[tuple[float, int]]] = [[sorted_prices[0]]]
    for p, t in sorted_prices[1:]:
        last = clusters[-1][-1][0]
        if abs(p - last) / max(abs(last), 1e-12) <= tolerance_pct:
            clusters[-1].append((p, t))
        else:
            clusters.append([(p, t)])
    out = []
    for cluster in clusters:
        low = min(p for p, _ in cluster)
        high = max(p for p, _ in cluster)
        last_t = max(t for _, t in cluster)
        out.append((low, high, len(cluster), last_t))
    return out


def support_resistance_zones(
    swings: Sequence[SwingPoint],
    tolerance_pct: float = 0.005,
    min_touches: int = 2,
) -> tuple[list[Zone], list[Zone]]:
    """Cluster swing highs → resistance zones, lows → support zones.

    `tolerance_pct` is the maximum fractional distance between neighbouring
    swings in the same cluster (0.005 = 0.5%). A zone is kept only when it
    contains at least `min_touches` swings.
    """
    highs = [(s.price, s.time_ms) for s in swings if s.kind == "high"]
    lows = [(s.price, s.time_ms) for s in swings if s.kind == "low"]
    resistance: list[Zone] = []
    for low, high, touches, last_t in _cluster_prices(highs, tolerance_pct):
        if touches >= min_touches:
            resistance.append(Zone("resistance", low, high, touches, last_t))
    support: list[Zone] = []
    for low, high, touches, last_t in _cluster_prices(lows, tolerance_pct):
        if touches >= min_touches:
            support.append(Zone("support", low, high, touches, last_t))
    return support, resistance


def nearest_zones(
    price: float, support: Sequence[Zone], resistance: Sequence[Zone]
) -> dict:
    """Pick the closest support below and resistance above `price`."""
    below = [z for z in support if z.high <= price]
    above = [z for z in resistance if z.low >= price]
    nearest_support = max(below, key=lambda z: z.high) if below else None
    nearest_resistance = min(above, key=lambda z: z.low) if above else None
    return {
        "nearest_support": nearest_support,
        "nearest_resistance": nearest_resistance,
    }
