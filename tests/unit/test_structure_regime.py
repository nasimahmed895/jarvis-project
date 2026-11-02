"""Phase 4 — structure + regime unit tests."""
from __future__ import annotations

import numpy as np

from market.analysis.multi_tf import analyse_timeframe, multi_tf_bias
from market.analysis.regime import classify
from market.analysis.structure import (
    find_swings,
    nearest_zones,
    support_resistance_zones,
)
from market.types import Candle, OHLCVFrame, Timeframe, TIMEFRAME_MS
from tests.fixtures.synth import (
    constant_frame,
    make_candle,
    sinusoidal_frame,
    trending_frame,
    zigzag_frame,
)


def test_find_swings_on_zigzag() -> None:
    # Alternating pivots at 100, 110, 100, 110, 100 → multiple swings.
    f = zigzag_frame([100.0, 110.0, 100.0, 110.0, 100.0], bars_between=5)
    swings = find_swings(f, k=2)
    assert any(s.kind == "high" for s in swings)
    assert any(s.kind == "low" for s in swings)


def test_find_swings_on_constant_frame_empty() -> None:
    f = constant_frame(50)
    assert find_swings(f, k=3) == []


def test_support_resistance_cluster() -> None:
    # Six pivots at the same two levels → one support, one resistance zone.
    prices = [100.0, 110.0, 100.0, 110.0, 100.0, 110.0, 100.0]
    f = zigzag_frame(prices, bars_between=5)
    swings = find_swings(f, k=2)
    supports, resistance = support_resistance_zones(swings, tolerance_pct=0.02, min_touches=2)
    assert len(supports) >= 1
    assert len(resistance) >= 1


def test_nearest_zones_order() -> None:
    prices = [100.0, 110.0, 100.0, 110.0, 100.0]
    f = zigzag_frame(prices, bars_between=5)
    swings = find_swings(f, k=2)
    s, r = support_resistance_zones(swings, tolerance_pct=0.02, min_touches=2)
    last_close = float(f.candles[-1].close)
    nz = nearest_zones(last_close, s, r)
    if nz["nearest_support"] is not None:
        assert nz["nearest_support"].high <= last_close
    if nz["nearest_resistance"] is not None:
        assert nz["nearest_resistance"].low >= last_close


def test_regime_uptrend() -> None:
    f = trending_frame(200, Timeframe.M1, start_price=100.0, step_price=1.0)
    snap = classify(f)
    assert snap.label in ("strong_uptrend", "weak_uptrend")
    assert "price" in snap.indicators


def test_regime_downtrend() -> None:
    f = trending_frame(200, Timeframe.M1, start_price=200.0, step_price=-1.0)
    snap = classify(f)
    assert snap.label in ("strong_downtrend", "weak_downtrend")


def test_regime_ranging() -> None:
    # A tight band around 100 with a bit of noise.
    rng = np.random.default_rng(1)
    noise = rng.normal(0, 0.3, 200)
    step = TIMEFRAME_MS[Timeframe.M1]
    start = 1_700_000_000_000
    candles = []
    for i, n in enumerate(noise):
        p = 100.0 + n
        candles.append(make_candle(start + i * step, Timeframe.M1, o=p, c=p + 0.05, h=p + 0.2, l=p - 0.2))
    f = OHLCVFrame("SYNTH", Timeframe.M1, "synth", start + 200 * step, tuple(candles))
    snap = classify(f)
    assert snap.label in ("ranging", "transition", "weak_uptrend", "weak_downtrend")


def test_regime_insufficient() -> None:
    f = constant_frame(10)
    snap = classify(f)
    assert snap.label == "uncertain"


def test_analyse_timeframe_shape() -> None:
    f = trending_frame(150, Timeframe.H1, start_price=100.0, step_price=0.3)
    report = analyse_timeframe(f, swing_k=3)
    assert report.timeframe == "1h"
    assert report.regime.label != "uncertain"


def test_multi_tf_long_bias() -> None:
    reports = [
        analyse_timeframe(trending_frame(150, Timeframe.H4, step_price=0.4))
        for _ in range(1)
    ] + [
        analyse_timeframe(trending_frame(150, Timeframe.H1, step_price=0.3))
    ] + [
        analyse_timeframe(trending_frame(150, Timeframe.M15, step_price=0.2))
    ]
    out = multi_tf_bias(reports)
    assert out["bias"] == "BUY BIAS"
    assert out["agreement"] is True


def test_multi_tf_conflict_no_trade() -> None:
    reports = [
        analyse_timeframe(trending_frame(150, Timeframe.H4, step_price=0.4)),
        analyse_timeframe(trending_frame(150, Timeframe.H1, start_price=200.0, step_price=-0.4)),
    ]
    out = multi_tf_bias(reports)
    assert out["bias"] == "NO TRADE"
    assert out["conflict"] is True
