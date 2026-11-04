"""Tests for actions/timezone_util.py."""
from __future__ import annotations

import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import actions.timezone_util as tz


def test_resolve_zone_canonical():
    z = tz._resolve_zone("Asia/Tokyo")
    assert str(z) == "Asia/Tokyo"


def test_resolve_zone_alias():
    z = tz._resolve_zone("JST")
    assert str(z) == "Asia/Tokyo"
    z2 = tz._resolve_zone("gmt")
    assert str(z2) == "UTC"


def test_resolve_zone_unknown_raises():
    try:
        tz._resolve_zone("Gondwana/Pangaea")
    except ValueError:
        return
    raise AssertionError("expected ValueError on unknown zone")


def test_convert_roundtrip_values():
    r = tz._convert("2027-01-15 10:00", "Asia/Tokyo", "UTC")
    assert "01:00" in r["target"]["time"]
    assert r["source"]["zone"] == "Asia/Tokyo"
    assert r["target"]["zone"] == "UTC"


def test_action_now_returns_zone_string():
    out = tz.timezone_util({"action": "now", "zone": "UTC"})
    assert "UTC" in out
    assert any(ch.isdigit() for ch in out)


def test_action_convert_formats_output():
    out = tz.timezone_util({
        "action": "convert",
        "when":   "2027-01-15 10:00",
        "from":   "America/Los_Angeles",
        "to":     "UTC",
    })
    assert "America/Los_Angeles" in out
    assert "UTC" in out


def test_action_convert_requires_fields():
    assert "'when'" in tz.timezone_util({"action": "convert", "from": "PT"})


def test_parse_attendees_short_spec():
    out = tz._parse_attendees("Asia/Tokyo:9-18, Europe/London:9-17")
    assert len(out) == 2
    assert out[0]["zone"] == "Asia/Tokyo"
    assert out[0]["start_hour"] == 9
    assert out[0]["end_hour"] == 18


def test_parse_attendees_default_hours():
    out = tz._parse_attendees("UTC")
    assert out[0]["start_hour"] == 9
    assert out[0]["end_hour"] == 17


def test_windows_finds_overlap_for_close_zones():
    # Two zones close to each other should overlap plenty.
    base = datetime(2027, 1, 20, 0, 0, tzinfo=timezone.utc)  # Wednesday
    windows = tz._overlap_windows(
        [{"zone": "Europe/London", "start_hour": 9, "end_hour": 17},
         {"zone": "Europe/Paris",  "start_hour": 9, "end_hour": 17}],
        duration_min=30,
        days=2,
        start=base,
    )
    assert windows, "expected at least one overlap window"


def test_windows_empty_for_impossible_hours():
    # Working hours that cover zero UTC hours in common.
    base = datetime(2027, 1, 20, 0, 0, tzinfo=timezone.utc)
    # Tokyo 1-2 AM (local) is mostly UTC afternoon prior day; LA 1-2 AM
    # (local) is UTC 9-10 AM. Together they rarely align, and never in
    # weekday hours. Narrow to both working 1-2 only.
    windows = tz._overlap_windows(
        [{"zone": "Asia/Tokyo",             "start_hour": 1, "end_hour": 2},
         {"zone": "America/Los_Angeles",    "start_hour": 1, "end_hour": 2}],
        duration_min=30, days=5, start=base,
    )
    assert windows == []


def test_action_windows_round_trip():
    out = tz.timezone_util({
        "action":       "windows",
        "attendees":    "UTC:9-17, Europe/London:9-17",
        "duration_min": 30,
        "days":         2,
    })
    assert "Shared working windows" in out


def test_action_rejects_unknown():
    assert "unknown action" in tz.timezone_util({"action": "warp"})


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as e:
            print(f"FAIL {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    sys.exit(1 if failed else 0)
