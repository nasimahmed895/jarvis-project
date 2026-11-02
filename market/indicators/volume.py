"""Volume indicators: relative volume, volume MA, OBV."""
from __future__ import annotations

import numpy as np

from .trend import sma


def relative_volume(volume: np.ndarray, lookback: int = 20) -> np.ndarray:
    """Current volume / mean volume over `lookback`. NaN during warm-up."""
    v = np.asarray(volume, dtype=np.float64)
    mean = sma(v, lookback)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = v / mean
    return out


def volume_ma(volume: np.ndarray, period: int = 20) -> np.ndarray:
    return sma(np.asarray(volume, dtype=np.float64), period)


def obv(close: np.ndarray, volume: np.ndarray) -> np.ndarray:
    """On-Balance Volume: cumulative signed volume."""
    c = np.asarray(close, dtype=np.float64)
    v = np.asarray(volume, dtype=np.float64)
    if c.size != v.size:
        raise ValueError("close and volume must be same length")
    out = np.zeros(c.size, dtype=np.float64)
    for i in range(1, c.size):
        if c[i] > c[i - 1]:
            out[i] = out[i - 1] + v[i]
        elif c[i] < c[i - 1]:
            out[i] = out[i - 1] - v[i]
        else:
            out[i] = out[i - 1]
    return out
