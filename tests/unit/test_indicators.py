"""Phase 4 — indicator unit tests."""
from __future__ import annotations

import numpy as np
import pytest

from market.indicators.momentum import macd, roc, rsi
from market.indicators.trend import adx, ema, sma
from market.indicators.volatility import atr, bollinger_bands, historical_volatility
from market.indicators.volume import obv, relative_volume


def test_sma_warmup() -> None:
    x = np.arange(10, dtype=float)
    s = sma(x, 3)
    assert np.isnan(s[:2]).all()
    # SMA of 0,1,2 = 1
    assert abs(s[2] - 1.0) < 1e-9
    # SMA of 7,8,9 = 8
    assert abs(s[-1] - 8.0) < 1e-9


def test_sma_bad_period() -> None:
    with pytest.raises(ValueError):
        sma(np.array([1.0, 2.0]), 0)


def test_ema_matches_sma_seed() -> None:
    x = np.array([1.0, 2.0, 3.0, 4.0, 5.0], dtype=float)
    e = ema(x, 3)
    # Seed = mean(1,2,3) = 2.0
    assert abs(e[2] - 2.0) < 1e-9
    # Monotonic increasing since input is increasing.
    assert e[-1] > e[-2] > e[-3]


def test_rsi_constant_series() -> None:
    x = np.full(50, 100.0)
    r = rsi(x, 14)
    # No gain, no loss → RSI undefined / 100 by convention when loss=0.
    assert r[-1] in (100.0, pytest.approx(100.0))


def test_rsi_monotonic_up() -> None:
    x = np.arange(1, 50, dtype=float)
    r = rsi(x, 14)
    # Pure uptrend → RSI 100 (no losses).
    assert r[-1] == pytest.approx(100.0)


def test_rsi_monotonic_down() -> None:
    x = np.arange(50, 1, -1, dtype=float)
    r = rsi(x, 14)
    # Pure downtrend → RSI 0.
    assert r[-1] == pytest.approx(0.0)


def test_macd_shape() -> None:
    x = np.sin(np.linspace(0, 6 * np.pi, 200)) * 10 + 100
    m, s, h = macd(x)
    assert m.size == 200 and s.size == 200 and h.size == 200
    # Last values must be finite.
    assert np.isfinite(m[-1]) and np.isfinite(s[-1])


def test_atr_positive() -> None:
    hi = np.array([10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25], dtype=float)
    lo = hi - 1
    cl = (hi + lo) / 2
    a = atr(hi, lo, cl, 14)
    assert np.isfinite(a[-1])
    assert a[-1] > 0


def test_bollinger_bands_cover_middle() -> None:
    x = np.linspace(100, 110, 50)
    u, m, l = bollinger_bands(x, 20, 2.0)
    assert np.all(u[19:] >= m[19:])
    assert np.all(l[19:] <= m[19:])


def test_hv_rising_with_noise() -> None:
    rng = np.random.default_rng(42)
    quiet = np.cumprod(1 + rng.normal(0, 0.001, 100)) * 100
    noisy = np.cumprod(1 + rng.normal(0, 0.02, 100)) * 100
    assert historical_volatility(noisy)[-1] > historical_volatility(quiet)[-1]


def test_obv_direction() -> None:
    c = np.array([10, 11, 10, 12, 13], dtype=float)
    v = np.array([1, 2, 3, 4, 5], dtype=float)
    o = obv(c, v)
    # Deltas: +, -, +, +
    # OBV: 0, +2, -1, +3, +8
    assert list(o) == [0.0, 2.0, -1.0, 3.0, 8.0]


def test_relative_volume_series() -> None:
    v = np.full(30, 10.0)
    rv = relative_volume(v, 20)
    # After warm-up: constant volume → rv == 1
    assert abs(rv[-1] - 1.0) < 1e-9


def test_adx_handles_short_series() -> None:
    hi = np.ones(10)
    lo = np.ones(10)
    cl = np.ones(10)
    a = adx(hi, lo, cl, 14)
    assert np.isnan(a).all()


def test_roc_warmup() -> None:
    x = np.arange(1, 20, dtype=float)
    r = roc(x, 5)
    assert np.isnan(r[:5]).all()
    assert np.isfinite(r[-1])
