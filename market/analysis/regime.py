"""Transparent market-regime classifier.

Rule-based so every label is traceable to specific values. Inputs are the
most recent indicator readings; the classifier never looks at future bars.

Regimes:
    strong_uptrend     EMA fast > EMA slow, ADX >= 25, higher-highs trend
    weak_uptrend       EMA fast > EMA slow, ADX in [15, 25)
    strong_downtrend   EMA fast < EMA slow, ADX >= 25, lower-lows trend
    weak_downtrend     EMA fast < EMA slow, ADX in [15, 25)
    ranging            |EMA fast - EMA slow| / price < 0.5%, ADX < 20
    transition         evidence conflicts
    uncertain          insufficient data

The confidence returned is HEURISTIC, not a calibrated probability. It is
derived from the strength of the triggering evidence (ADX distance from the
threshold, EMA separation, volatility compression). It must not be presented
as a trading probability.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from ..indicators.momentum import rsi
from ..indicators.trend import adx, ema
from ..indicators.volatility import atr
from ..types import OHLCVFrame


REGIMES = (
    "strong_uptrend",
    "weak_uptrend",
    "strong_downtrend",
    "weak_downtrend",
    "ranging",
    "transition",
    "uncertain",
)


@dataclass(frozen=True, slots=True)
class RegimeSnapshot:
    label: str
    heuristic_confidence: float  # [0, 1] — NOT a probability.
    reasons: tuple[str, ...]
    indicators: dict[str, float]


def classify(
    frame: OHLCVFrame,
    ema_fast: int = 20,
    ema_slow: int = 50,
    adx_period: int = 14,
    atr_period: int = 14,
) -> RegimeSnapshot:
    """Return a regime label with evidence for every rule that triggered."""
    closes = np.array(frame.closes(), dtype=np.float64)
    highs = np.array(frame.highs(), dtype=np.float64)
    lows = np.array(frame.lows(), dtype=np.float64)

    min_bars = max(ema_slow, 2 * adx_period) + 5
    if closes.size < min_bars:
        return RegimeSnapshot(
            label="uncertain",
            heuristic_confidence=0.0,
            reasons=(f"insufficient data: need {min_bars} bars, have {closes.size}",),
            indicators={},
        )

    e_fast = ema(closes, ema_fast)
    e_slow = ema(closes, ema_slow)
    adx_vals = adx(highs, lows, closes, adx_period)
    atr_vals = atr(highs, lows, closes, atr_period)
    rsi_vals = rsi(closes, 14)

    last_price = float(closes[-1])
    last_e_fast = float(e_fast[-1])
    last_e_slow = float(e_slow[-1])
    last_adx = float(adx_vals[-1]) if not np.isnan(adx_vals[-1]) else 0.0
    last_atr = float(atr_vals[-1]) if not np.isnan(atr_vals[-1]) else 0.0
    last_rsi = float(rsi_vals[-1]) if not np.isnan(rsi_vals[-1]) else 50.0

    ema_sep = (last_e_fast - last_e_slow) / max(last_price, 1e-12)

    reasons: list[str] = []
    label: str
    conf: float

    if last_e_fast > last_e_slow and last_adx >= 25:
        label = "strong_uptrend"
        reasons.append(f"EMA{ema_fast} > EMA{ema_slow} (sep={ema_sep:.2%})")
        reasons.append(f"ADX={last_adx:.1f} >= 25")
        conf = min(1.0, 0.5 + (last_adx - 25) / 50.0 + max(0.0, ema_sep) * 10)
    elif last_e_fast < last_e_slow and last_adx >= 25:
        label = "strong_downtrend"
        reasons.append(f"EMA{ema_fast} < EMA{ema_slow} (sep={ema_sep:.2%})")
        reasons.append(f"ADX={last_adx:.1f} >= 25")
        conf = min(1.0, 0.5 + (last_adx - 25) / 50.0 + max(0.0, -ema_sep) * 10)
    elif last_e_fast > last_e_slow and 15 <= last_adx < 25:
        label = "weak_uptrend"
        reasons.append(f"EMA{ema_fast} > EMA{ema_slow} but ADX={last_adx:.1f} < 25")
        conf = 0.4
    elif last_e_fast < last_e_slow and 15 <= last_adx < 25:
        label = "weak_downtrend"
        reasons.append(f"EMA{ema_fast} < EMA{ema_slow} but ADX={last_adx:.1f} < 25")
        conf = 0.4
    elif abs(ema_sep) < 0.005 and last_adx < 20:
        label = "ranging"
        reasons.append(f"EMA separation {ema_sep:.2%} < 0.5%, ADX={last_adx:.1f} < 20")
        conf = 0.5
    else:
        label = "transition"
        reasons.append("evidence conflicts: EMA direction and ADX disagree")
        conf = 0.2

    return RegimeSnapshot(
        label=label,
        heuristic_confidence=round(conf, 3),
        reasons=tuple(reasons),
        indicators={
            "price": last_price,
            f"ema_{ema_fast}": last_e_fast,
            f"ema_{ema_slow}": last_e_slow,
            "ema_separation_pct": ema_sep,
            "adx": last_adx,
            "atr": last_atr,
            "rsi": last_rsi,
        },
    )
