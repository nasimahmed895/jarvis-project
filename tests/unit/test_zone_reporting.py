"""Zone reporting regression tests.

A missing nearest zone must state WHY it is missing and never silently
appear as `n/a` in the JSON. A distant zone must label itself as such.
"""
from __future__ import annotations

from market.analysis.structure import Zone
from market.reports.analysis_report import _zone_payload


def test_missing_zone_states_reason() -> None:
    p = _zone_payload(None, ref_price=100.0, kind="support")
    assert p["available"] is False
    assert "no clustered support zone" in p["reason"]
    assert p["distance_pct"] is None


def test_present_zone_includes_distance() -> None:
    z = Zone("resistance", 110.0, 112.0, touches=3, last_touch_time_ms=0)
    p = _zone_payload(z, ref_price=100.0, kind="resistance")
    assert p["available"] is True
    assert p["range"] == [110.0, 112.0]
    assert p["touches"] == 3
    # 111 is 11% above 100 → flagged as far.
    assert "far" in p["note"]


def test_near_zone_not_flagged() -> None:
    z = Zone("resistance", 100.5, 101.5, touches=2, last_touch_time_ms=0)
    p = _zone_payload(z, ref_price=100.0, kind="resistance")
    assert p["available"] is True
    assert "far" not in p["note"]
