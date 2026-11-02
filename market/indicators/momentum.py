"""Momentum indicators: RSI, MACD, ROC.

All functions return numpy arrays with NaN during the warm-up window.
"""
from __future__ import annotations

import numpy as np

from .trend import ema


def rsi(values: np.ndarray, period: int = 14) -> np.ndarray:
    """Wilder's RSI (0..100). First value available at index `period`."""
    if period <= 0:
        raise ValueError("period must be positive")
    x = np.asarray(values, dtype=np.float64)
    n = x.size
    out = np.full(n, np.nan, dtype=np.float64)
    if n <= period:
        return out
    deltas = np.diff(x)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = float(np.mean(gains[:period]))
    avg_loss = float(np.mean(losses[:period]))
    rs = avg_gain / avg_loss if avg_loss > 0 else np.inf
    out[period] = 100.0 - 100.0 / (1.0 + rs) if np.isfinite(rs) else 100.0
    for i in range(period + 1, n):
        g = gains[i - 1]
        l = losses[i - 1]
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
        if avg_loss == 0:
            out[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            out[i] = 100.0 - 100.0 / (1.0 + rs)
    return out


def macd(
    values: np.ndarray,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return (macd_line, signal_line, histogram)."""
    if not (0 < fast < slow):
        raise ValueError("fast must be positive and less than slow")
    e_fast = ema(values, fast)
    e_slow = ema(values, slow)
    macd_line = e_fast - e_slow
    signal_line = ema(np.nan_to_num(macd_line, nan=0.0), signal)
    # Mask warm-up region: signal_line is only meaningful once both EMAs have
    # warmed up for `signal` additional bars.
    warm = slow + signal - 1
    signal_line = signal_line.copy()
    signal_line[:warm] = np.nan
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def roc(values: np.ndarray, period: int = 10) -> np.ndarray:
    """Rate of change as a percentage."""
    if period <= 0:
        raise ValueError("period must be positive")
    x = np.asarray(values, dtype=np.float64)
    n = x.size
    out = np.full(n, np.nan, dtype=np.float64)
    if n <= period:
        return out
    for i in range(period, n):
        if x[i - period] == 0:
            out[i] = np.nan
        else:
            out[i] = (x[i] - x[i - period]) / x[i - period] * 100.0
    return out
