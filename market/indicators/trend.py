"""Trend indicators: SMA, EMA, ADX.

All functions accept a 1-D numpy float array of closes (or highs/lows for
ADX). Output arrays are the same length as the input; the warm-up prefix is
filled with NaN so the caller can distinguish "no value yet" from a real
reading. No look-ahead: the value at index `i` depends only on indices
`<= i`.
"""
from __future__ import annotations

import numpy as np


def sma(values: np.ndarray, period: int) -> np.ndarray:
    """Simple moving average."""
    if period <= 0:
        raise ValueError("period must be positive")
    x = np.asarray(values, dtype=np.float64)
    n = x.size
    out = np.full(n, np.nan, dtype=np.float64)
    if n < period:
        return out
    cumsum = np.cumsum(np.insert(x, 0, 0.0))
    sums = cumsum[period:] - cumsum[:-period]
    out[period - 1 :] = sums / period
    return out


def ema(values: np.ndarray, period: int) -> np.ndarray:
    """Exponential moving average seeded with the SMA of the first `period`."""
    if period <= 0:
        raise ValueError("period must be positive")
    x = np.asarray(values, dtype=np.float64)
    n = x.size
    out = np.full(n, np.nan, dtype=np.float64)
    if n < period:
        return out
    alpha = 2.0 / (period + 1.0)
    seed = float(np.mean(x[:period]))
    out[period - 1] = seed
    for i in range(period, n):
        out[i] = alpha * x[i] + (1.0 - alpha) * out[i - 1]
    return out


def _true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    h = np.asarray(high, dtype=np.float64)
    l = np.asarray(low, dtype=np.float64)
    c = np.asarray(close, dtype=np.float64)
    prev_close = np.concatenate(([c[0]], c[:-1]))
    tr = np.maximum.reduce([
        h - l,
        np.abs(h - prev_close),
        np.abs(l - prev_close),
    ])
    tr[0] = h[0] - l[0]
    return tr


def _wilder_smooth(values: np.ndarray, period: int) -> np.ndarray:
    """Wilder's smoothing (used by ADX / RSI / ATR per Wilder's definitions)."""
    x = np.asarray(values, dtype=np.float64)
    n = x.size
    out = np.full(n, np.nan, dtype=np.float64)
    if n < period:
        return out
    out[period - 1] = float(np.sum(x[:period]))
    for i in range(period, n):
        out[i] = out[i - 1] - (out[i - 1] / period) + x[i]
    return out


def adx(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14
) -> np.ndarray:
    """Average Directional Index (0..100).

    Implementation follows Wilder's 1978 definition — directional movement
    sums are Wilder-smoothed, the DX line is 100 * |+DI - -DI| / (+DI + -DI),
    and the ADX is Wilder-smoothed DX.
    """
    h = np.asarray(high, dtype=np.float64)
    l = np.asarray(low, dtype=np.float64)
    c = np.asarray(close, dtype=np.float64)
    n = h.size
    out = np.full(n, np.nan, dtype=np.float64)
    if n < 2 * period:
        return out

    up_move = np.diff(h, prepend=h[0])
    down_move = np.diff(-l, prepend=-l[0])  # == l[i-1] - l[i]
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    plus_dm[0] = 0.0
    minus_dm[0] = 0.0

    tr = _true_range(h, l, c)
    tr_smooth = _wilder_smooth(tr, period)
    plus_smooth = _wilder_smooth(plus_dm, period)
    minus_smooth = _wilder_smooth(minus_dm, period)

    with np.errstate(divide="ignore", invalid="ignore"):
        plus_di = 100.0 * plus_smooth / tr_smooth
        minus_di = 100.0 * minus_smooth / tr_smooth
        dx = 100.0 * np.abs(plus_di - minus_di) / (plus_di + minus_di)
    dx = np.nan_to_num(dx, nan=0.0, posinf=0.0, neginf=0.0)

    # First ADX = simple mean of first `period` DX values; then Wilder-smoothed.
    out[2 * period - 1] = float(np.mean(dx[period - 1 : 2 * period - 1 + 1]))
    for i in range(2 * period, n):
        out[i] = (out[i - 1] * (period - 1) + dx[i]) / period
    return out
