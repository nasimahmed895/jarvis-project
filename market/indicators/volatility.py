"""Volatility indicators: ATR, Bollinger Bands, historical volatility."""
from __future__ import annotations

import numpy as np

from .trend import _true_range, sma


def atr(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14
) -> np.ndarray:
    """Wilder's ATR."""
    tr = _true_range(high, low, close)
    n = tr.size
    out = np.full(n, np.nan, dtype=np.float64)
    if n < period:
        return out
    out[period - 1] = float(np.mean(tr[:period]))
    for i in range(period, n):
        out[i] = (out[i - 1] * (period - 1) + tr[i]) / period
    return out


def bollinger_bands(
    values: np.ndarray, period: int = 20, num_std: float = 2.0
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return (upper, middle, lower)."""
    if period <= 1:
        raise ValueError("period must be greater than 1")
    x = np.asarray(values, dtype=np.float64)
    n = x.size
    middle = sma(x, period)
    upper = np.full(n, np.nan, dtype=np.float64)
    lower = np.full(n, np.nan, dtype=np.float64)
    for i in range(period - 1, n):
        window = x[i - period + 1 : i + 1]
        std = float(np.std(window, ddof=0))
        upper[i] = middle[i] + num_std * std
        lower[i] = middle[i] - num_std * std
    return upper, middle, lower


def historical_volatility(values: np.ndarray, period: int = 20) -> np.ndarray:
    """Rolling std of log returns, annualised to a daily basis (sqrt(365))."""
    x = np.asarray(values, dtype=np.float64)
    n = x.size
    out = np.full(n, np.nan, dtype=np.float64)
    if n <= period:
        return out
    with np.errstate(divide="ignore", invalid="ignore"):
        rets = np.diff(np.log(np.clip(x, 1e-12, None)))
    for i in range(period, n):
        window = rets[i - period : i]
        out[i] = float(np.std(window, ddof=0)) * np.sqrt(365.0)
    return out
