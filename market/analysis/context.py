"""Broader-market context: BTC trend / correlation / market breadth.

Every function returns an explicit `available` flag so the report layer can
state "not available" instead of hiding gaps. News is intentionally not
implemented in v1 and is reported as unavailable.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..indicators.trend import ema
from ..interfaces import DataProvider
from ..types import OHLCVFrame, Timeframe


@dataclass(frozen=True, slots=True)
class ContextResult:
    name: str
    available: bool
    value: dict | None = None
    reason: str = ""


def btc_trend_snapshot(provider: DataProvider, timeframe: Timeframe = Timeframe.D1) -> ContextResult:
    """BTC trend summary — direction of EMA20 vs EMA50 on BTC.

    Returns `available=False` when the provider cannot deliver the data or
    when the series is too short to classify. Never fabricates a value.
    """
    try:
        frame = provider.ohlcv("BTC", timeframe, limit=200)
    except Exception as e:  # noqa: BLE001
        return ContextResult("btc_trend", False, reason=f"provider error: {type(e).__name__}")
    closes = np.array(frame.closes(), dtype=np.float64)
    if closes.size < 60:
        return ContextResult("btc_trend", False, reason="insufficient data")
    e_fast = ema(closes, 20)
    e_slow = ema(closes, 50)
    if np.isnan(e_fast[-1]) or np.isnan(e_slow[-1]):
        return ContextResult("btc_trend", False, reason="indicators not warmed up")
    direction = "up" if e_fast[-1] > e_slow[-1] else "down"
    return ContextResult(
        "btc_trend",
        True,
        value={
            "direction": direction,
            "timeframe": timeframe.value,
            "ema20": float(e_fast[-1]),
            "ema50": float(e_slow[-1]),
            "last_close": float(closes[-1]),
            "last_candle_time_ms": frame.candles[-1].close_time_ms if frame.candles else 0,
            "source": frame.source,
            "retrieved_at_ms": frame.retrieved_at_ms,
            "note": (
                "This is BTC's own trend on its own timeframe. A conflict with the "
                "analysed asset's lower-timeframe regime is a legitimate difference, "
                "not a bug."
            ),
        },
    )


def correlation_with_btc(
    provider: DataProvider,
    asset: str,
    timeframe: Timeframe = Timeframe.D1,
    bars: int = 90,
) -> ContextResult:
    """Pearson correlation of daily log returns between `asset` and BTC.

    Correlation does not imply causation; the value is a descriptive summary
    of co-movement over the lookback. Reported as unavailable when either
    series is insufficient.
    """
    try:
        btc = provider.ohlcv("BTC", timeframe, limit=bars + 5)
        alt = provider.ohlcv(asset, timeframe, limit=bars + 5)
    except Exception as e:  # noqa: BLE001
        return ContextResult(
            "correlation_with_btc", False, reason=f"provider error: {type(e).__name__}"
        )
    btc_closes = np.array(btc.closes(), dtype=np.float64)
    alt_closes = np.array(alt.closes(), dtype=np.float64)
    if btc_closes.size < bars + 1 or alt_closes.size < bars + 1:
        return ContextResult("correlation_with_btc", False, reason="insufficient data")
    # Align tails.
    n = min(btc_closes.size, alt_closes.size)
    btc_closes = btc_closes[-n:]
    alt_closes = alt_closes[-n:]
    with np.errstate(divide="ignore", invalid="ignore"):
        btc_rets = np.diff(np.log(np.clip(btc_closes, 1e-12, None)))
        alt_rets = np.diff(np.log(np.clip(alt_closes, 1e-12, None)))
    if btc_rets.size < 10 or alt_rets.size < 10:
        return ContextResult("correlation_with_btc", False, reason="insufficient returns")
    r = float(np.corrcoef(btc_rets, alt_rets)[0, 1])
    return ContextResult(
        "correlation_with_btc",
        True,
        value={
            "asset": asset.upper(),
            "timeframe": timeframe.value,
            "bars": bars,
            "pearson_r": r,
            "note": "Correlation does not imply causation; may change over time.",
            "source": btc.source,
        },
    )


def news_sentiment(asset: str) -> ContextResult:
    """News module is intentionally absent in v1.

    Returning `available=False` is the honest answer; adding a half-working
    news scraper would risk showing fabricated or stale headlines as current
    market context. Phase 5 explicitly skips this per the agreed scope.
    """
    return ContextResult(
        "news_sentiment",
        False,
        reason="news module not implemented in v1 (deliberate; see Phase 5 scope)",
    )
