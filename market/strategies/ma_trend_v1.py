"""Baseline strategy: moving-average trend + momentum confirmation (v1).

RULES — immutable for this version. If any rule changes, bump the version.

Entry (BUY BIAS):
    1. EMA20 > EMA50 (uptrend baseline)
    2. ADX(14) >= 20 (trend has some strength)
    3. RSI(14) between 45 and 70 (not oversold reversal, not blow-off top)
    4. Last close > EMA20 (price above fast MA, consistent with the trend)
    5. Last close is NOT within 0.3% of a resistance zone (gives room to run)

Entry (SHORT BIAS):
    Same rules mirrored: EMA20 < EMA50, ADX >= 20, RSI between 30 and 55,
    close < EMA20, not within 0.3% of a support zone.

Otherwise:
    NO TRADE.

Stop-loss:  entry minus (plus) 1.5 * ATR(14) for long (short).
Target:     entry plus (minus) 2.5 * ATR(14) for long (short). RR ≈ 1.67.
Entry:      the last close is advisory — use "pullback to EMA20 while
            indicators remain in the stated bands" as a condition-based entry.

Known weaknesses:
- Trades the obvious trend only; will give back in rapid reversals.
- Ignores order-book, funding, and macro news.
- ADX uses Wilder smoothing, which lags.
- Backtests with fees/slippage are required before any live use.
"""
from __future__ import annotations

from typing import Any

import numpy as np

from ..analysis.structure import find_swings, support_resistance_zones
from ..indicators.momentum import rsi
from ..indicators.trend import adx, ema
from ..indicators.volatility import atr
from ..types import OHLCVFrame, Timeframe
from .base import (
    EntryCondition,
    LONG_BIAS,
    NO_TRADE,
    SHORT_BIAS,
    Strategy,
    StrategyDecision,
)
from .registry import register


class MATrendMomentumV1(Strategy):
    name = "ma_trend_momentum"
    version = "1.0.0"
    supported_timeframes = (
        Timeframe.M15,
        Timeframe.M30,
        Timeframe.H1,
        Timeframe.H4,
        Timeframe.D1,
    )
    min_bars = 80

    def evaluate(self, frame: OHLCVFrame, context: dict[str, Any]) -> StrategyDecision:
        candles = frame.candles
        n = len(candles)
        last_close = float(candles[-1].close) if candles else 0.0
        base_kwargs = dict(
            strategy_name=self.name,
            strategy_version=self.version,
            asset=frame.symbol,
            timeframe=frame.timeframe.value,
            source=frame.source,
            analysis_ts_ms=frame.retrieved_at_ms,
            reference_price=last_close,
        )
        if n < self.min_bars:
            return StrategyDecision(
                decision=NO_TRADE,
                evidence=(f"insufficient data: {n} < {self.min_bars}",),
                **base_kwargs,
            )

        closes = np.array(frame.closes(), dtype=np.float64)
        highs = np.array(frame.highs(), dtype=np.float64)
        lows = np.array(frame.lows(), dtype=np.float64)

        e20 = ema(closes, 20)
        e50 = ema(closes, 50)
        adx_v = adx(highs, lows, closes, 14)
        rsi_v = rsi(closes, 14)
        atr_v = atr(highs, lows, closes, 14)

        last_e20 = float(e20[-1])
        last_e50 = float(e50[-1])
        last_adx = float(adx_v[-1]) if not np.isnan(adx_v[-1]) else 0.0
        last_rsi = float(rsi_v[-1]) if not np.isnan(rsi_v[-1]) else 50.0
        last_atr = float(atr_v[-1]) if not np.isnan(atr_v[-1]) else 0.0

        swings = find_swings(frame, k=3)
        supports, resistance = support_resistance_zones(
            swings, tolerance_pct=0.005, min_touches=2
        )

        evidence: list[str] = []
        conflicts: list[str] = []

        long_rules = (
            last_e20 > last_e50,
            last_adx >= 20.0,
            45.0 <= last_rsi <= 70.0,
            last_close > last_e20,
        )
        short_rules = (
            last_e20 < last_e50,
            last_adx >= 20.0,
            30.0 <= last_rsi <= 55.0,
            last_close < last_e20,
        )

        decision = NO_TRADE
        entry: EntryCondition | None = None
        stop_loss: float | None = None
        targets: tuple[float, ...] = ()
        rr_estimate: float | None = None

        if all(long_rules):
            # Headroom check vs nearest resistance.
            near_r = min(
                (z for z in resistance if z.low >= last_close),
                key=lambda z: z.low,
                default=None,
            )
            if near_r is not None and (near_r.low - last_close) / max(last_close, 1e-9) < 0.003:
                conflicts.append(
                    f"nearest resistance zone at {near_r.low:.4f}-{near_r.high:.4f} within 0.3%"
                )
                evidence.append("long rules met except headroom-vs-resistance")
                decision = NO_TRADE
            elif last_atr <= 0.0:
                conflicts.append("ATR not available; cannot size stop")
                decision = NO_TRADE
            else:
                decision = LONG_BIAS
                evidence.append(f"EMA20={last_e20:.4f} > EMA50={last_e50:.4f}")
                evidence.append(f"ADX={last_adx:.1f} >= 20")
                evidence.append(f"RSI={last_rsi:.1f} in [45, 70]")
                evidence.append(f"close={last_close:.4f} > EMA20")
                stop_loss = last_close - 1.5 * last_atr
                target = last_close + 2.5 * last_atr
                targets = (target,)
                entry = EntryCondition(
                    kind="condition",
                    description="Pullback to EMA20 while EMA20>EMA50, ADX>=20, RSI in [45,70].",
                )
                rr_estimate = round(2.5 / 1.5, 3)
        elif all(short_rules):
            near_s = max(
                (z for z in supports if z.high <= last_close),
                key=lambda z: z.high,
                default=None,
            )
            if near_s is not None and (last_close - near_s.high) / max(last_close, 1e-9) < 0.003:
                conflicts.append(
                    f"nearest support zone at {near_s.low:.4f}-{near_s.high:.4f} within 0.3%"
                )
                decision = NO_TRADE
            elif last_atr <= 0.0:
                conflicts.append("ATR not available; cannot size stop")
                decision = NO_TRADE
            else:
                decision = SHORT_BIAS
                evidence.append(f"EMA20={last_e20:.4f} < EMA50={last_e50:.4f}")
                evidence.append(f"ADX={last_adx:.1f} >= 20")
                evidence.append(f"RSI={last_rsi:.1f} in [30, 55]")
                evidence.append(f"close={last_close:.4f} < EMA20")
                stop_loss = last_close + 1.5 * last_atr
                target = last_close - 2.5 * last_atr
                targets = (target,)
                entry = EntryCondition(
                    kind="condition",
                    description="Rally back to EMA20 while EMA20<EMA50, ADX>=20, RSI in [30,55].",
                )
                rr_estimate = round(2.5 / 1.5, 3)
        else:
            if last_e20 > last_e50:
                evidence.append("uptrend MAs but confirmation incomplete")
            elif last_e20 < last_e50:
                evidence.append("downtrend MAs but confirmation incomplete")
            else:
                evidence.append("MAs flat")
            conflicts.append(
                f"ADX={last_adx:.1f}, RSI={last_rsi:.1f}, close-to-EMA20={last_close - last_e20:.4f}"
            )

        if decision == NO_TRADE:
            heuristic_conf = 0.1
        else:
            # Heuristic score — bounded, NOT a probability.
            heuristic_conf = round(
                min(
                    1.0,
                    0.3 + (last_adx - 20) / 60.0 + (0.5 - abs(last_rsi - 57.5) / 25.0),
                ),
                3,
            )

        return StrategyDecision(
            decision=decision,
            entry=entry,
            stop_loss=stop_loss,
            targets=targets,
            rr_estimate=rr_estimate,
            evidence=tuple(evidence),
            conflicts=tuple(conflicts),
            cancellation_conditions=(
                "EMA20 crosses back through EMA50 against the bias.",
                "ADX falls below 15.",
                "Close returns to the opposite side of EMA20 for 3+ bars.",
            ),
            heuristic_confidence=max(0.0, heuristic_conf),
            extras={
                "ema20": last_e20,
                "ema50": last_e50,
                "adx": last_adx,
                "rsi": last_rsi,
                "atr": last_atr,
            },
            **base_kwargs,
        )


register(MATrendMomentumV1())
