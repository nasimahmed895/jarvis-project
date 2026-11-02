"""Nearest-zone ranking determinism.

Given multiple zones on each side of the reference price, the resolver must
always pick the one closest to the reference and the choice must be stable.
"""
from __future__ import annotations

from market.analysis.structure import Zone, nearest_zones


def test_nearest_support_is_the_highest_below_ref() -> None:
    supports = [
        Zone("support", 85, 86, touches=2, last_touch_time_ms=0),
        Zone("support", 90, 91, touches=3, last_touch_time_ms=0),
        Zone("support", 95, 95.5, touches=2, last_touch_time_ms=0),
    ]
    out = nearest_zones(100.0, supports, [])
    assert out["nearest_support"].high == 95.5


def test_nearest_resistance_is_the_lowest_above_ref() -> None:
    resistance = [
        Zone("resistance", 110, 111, touches=2, last_touch_time_ms=0),
        Zone("resistance", 105, 106, touches=3, last_touch_time_ms=0),
        Zone("resistance", 120, 122, touches=5, last_touch_time_ms=0),
    ]
    out = nearest_zones(100.0, [], resistance)
    assert out["nearest_resistance"].low == 105


def test_no_zones_returns_none() -> None:
    out = nearest_zones(100.0, [], [])
    assert out["nearest_support"] is None
    assert out["nearest_resistance"] is None


def test_zones_above_ignored_for_support() -> None:
    supports = [Zone("support", 105, 106, touches=2, last_touch_time_ms=0)]
    out = nearest_zones(100.0, supports, [])
    assert out["nearest_support"] is None


def test_zones_below_ignored_for_resistance() -> None:
    resistance = [Zone("resistance", 95, 96, touches=2, last_touch_time_ms=0)]
    out = nearest_zones(100.0, [], resistance)
    assert out["nearest_resistance"] is None
