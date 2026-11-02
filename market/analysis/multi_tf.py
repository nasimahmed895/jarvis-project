"""Multi-timeframe analysis.

Given frames at a higher timeframe (bias), intermediate (structure), and
lower (entry trigger), summarise agreement/disagreement across them.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..types import OHLCVFrame
from .regime import RegimeSnapshot, classify
from .structure import (
    SwingPoint,
    Zone,
    find_swings,
    nearest_zones,
    support_resistance_zones,
)


@dataclass(frozen=True, slots=True)
class TimeframeReport:
    timeframe: str
    regime: RegimeSnapshot
    swings: tuple[SwingPoint, ...]
    support: tuple[Zone, ...]
    resistance: tuple[Zone, ...]
    nearest_support: Zone | None
    nearest_resistance: Zone | None


def analyse_timeframe(frame: OHLCVFrame, swing_k: int = 3) -> TimeframeReport:
    regime = classify(frame)
    swings = find_swings(frame, k=swing_k)
    support, resistance = support_resistance_zones(swings)
    last_price = frame.candles[-1].close if frame.candles else 0.0
    near = nearest_zones(last_price, support, resistance)
    return TimeframeReport(
        timeframe=frame.timeframe.value,
        regime=regime,
        swings=tuple(swings),
        support=tuple(support),
        resistance=tuple(resistance),
        nearest_support=near["nearest_support"],
        nearest_resistance=near["nearest_resistance"],
    )


_DIRECTIONAL = {
    "strong_uptrend": +1,
    "weak_uptrend": +1,
    "strong_downtrend": -1,
    "weak_downtrend": -1,
    "ranging": 0,
    "transition": 0,
    "uncertain": 0,
}


def multi_tf_bias(reports: list[TimeframeReport]) -> dict[str, Any]:
    """Combine regime labels across timeframes into a bias summary."""
    directions = [_DIRECTIONAL[r.regime.label] for r in reports]
    agree = all(d == directions[0] for d in directions) and directions[0] != 0
    conflict = any(a * b < 0 for a in directions for b in directions)

    if agree and directions[0] > 0:
        bias = "BUY BIAS"
    elif agree and directions[0] < 0:
        bias = "SHORT BIAS"
    elif conflict:
        bias = "NO TRADE"
    elif all(d == 0 for d in directions):
        bias = "NO TRADE"
    else:
        bias = "NO TRADE"

    return {
        "bias": bias,
        "agreement": agree,
        "conflict": conflict,
        "per_tf": [
            {"timeframe": r.timeframe, "regime": r.regime.label}
            for r in reports
        ],
    }
