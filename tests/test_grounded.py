"""Tests for core/grounded.py + actions/grounded.py.

Network calls (requests.get, web_search._search) are mocked throughout
so the suite stays deterministic and offline.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import grounded as g
import actions.grounded as g_action


def _reset_cache():
    g._CACHE.clear()


def test_time_now_is_verified():
    c = g.time_now().to_dict()
    assert c["tag"] == "verified"
    assert c["confidence"] == 1.0
    assert "system-clock" in c["sources"][0]


def test_time_now_specific_zone():
    c = g.time_now("Asia/Tokyo").to_dict()
    assert c["tag"] == "verified"
    assert "Asia/Tokyo" in c["sources"][0]


def test_location_without_city_and_no_memory():
    _reset_cache()
    with mock.patch.object(g, "user_city", return_value=None):
        c = g.location().to_dict()
    assert c["tag"] == "unknown"


def test_location_falls_back_to_estimated_on_geocode_fail():
    _reset_cache()
    with mock.patch.object(g, "_geocode", return_value=None):
        c = g.location("Atlantis").to_dict()
    assert c["tag"] == "estimated"


def test_location_verified_with_geocode():
    _reset_cache()
    fake = {"lat": 35.68, "lon": 139.76, "name": "Tokyo",
            "country": "Japan", "timezone": "Asia/Tokyo"}
    with mock.patch.object(g, "_geocode", return_value=fake):
        c = g.location("Tokyo").to_dict()
    assert c["tag"] == "verified"
    assert "Tokyo" in c["text"]
    assert c["sources"] == ["open-meteo-geocoding"]


def test_weather_unknown_without_city():
    _reset_cache()
    with mock.patch.object(g, "user_city", return_value=None):
        c = g.weather().to_dict()
    assert c["tag"] == "unknown"


def test_weather_verified_with_mock_api():
    _reset_cache()
    fake_geo = {"lat": 1.3, "lon": 103.8, "name": "Singapore",
                "country": "Singapore", "timezone": "Asia/Singapore"}
    fake_wx = {
        "temperature_2m": 29.5,
        "relative_humidity_2m": 80,
        "weather_code": 61,
        "wind_speed_10m": 12,
    }

    class _R:
        status_code = 200
        def json(self):
            return {"current": fake_wx}

    with mock.patch.object(g, "_geocode", return_value=fake_geo), \
         mock.patch.object(g, "requests", create=True) as req:
        req.get.return_value = _R()
        c = g.weather("Singapore").to_dict()
    assert c["tag"] == "verified"
    assert "Singapore" in c["text"]
    assert "light rain" in c["text"]
    assert c["sources"] == ["open-meteo-forecast"]


def test_weather_marks_unknown_on_http_error():
    _reset_cache()
    fake_geo = {"lat": 0, "lon": 0, "name": "X",
                "country": "", "timezone": "UTC"}

    class _R:
        status_code = 503
        def json(self):
            return {}

    with mock.patch.object(g, "_geocode", return_value=fake_geo), \
         mock.patch.object(g, "requests", create=True) as req:
        req.get.return_value = _R()
        c = g.weather("X").to_dict()
    assert c["tag"] == "unknown"


def test_fact_check_unknown_when_no_sources():
    with mock.patch.object(g, "_search_sources", return_value=[]):
        c = g.fact_check("anything").to_dict()
    assert c["tag"] == "unknown"


def test_fact_check_verified_requires_two_distinct_domains():
    srcs = [
        {"url": "https://nasa.gov/story/a"},
        {"url": "https://wikipedia.org/wiki/b"},
    ]
    with mock.patch.object(g, "_search_sources", return_value=srcs):
        c = g.fact_check("the moon exists").to_dict()
    assert c["tag"] == "verified"
    assert c["confidence"] >= 0.5


def test_fact_check_estimated_with_single_domain():
    srcs = [
        {"url": "https://example.com/a"},
        {"url": "https://example.com/b"},
    ]
    with mock.patch.object(g, "_search_sources", return_value=srcs):
        c = g.fact_check("x").to_dict()
    assert c["tag"] == "estimated"


def test_cross_check_returns_one_claim_per_input():
    with mock.patch.object(
        g, "fact_check",
        side_effect=lambda claim: g.Claim(text=claim, tag="verified"),
    ):
        results = g.cross_check(["a", "b", "c"])
    assert len(results) == 3
    assert [r.text for r in results] == ["a", "b", "c"]


def test_situational_snapshot_has_three_blocks():
    _reset_cache()
    with mock.patch.object(g, "user_city", return_value=None):
        snap = g.situational()
    assert set(snap.keys()) == {"time", "location", "weather"}
    assert snap["time"]["tag"] == "verified"


# ── Action layer ────────────────────────────────────────────────────────────

def test_action_now_renders_claim():
    out = g_action.grounded({"action": "now"})
    assert "[verified" in out
    assert "Current time" in out


def test_action_fact_check_requires_claim():
    out = g_action.grounded({"action": "fact_check"})
    assert "'claim'" in out


def test_action_snapshot_renders_three_claims():
    _reset_cache()
    with mock.patch.object(g, "user_city", return_value=None):
        out = g_action.grounded({"action": "snapshot"})
    assert "Situational snapshot" in out
    assert out.count("[") >= 3  # three claim lines


def test_action_rejects_unknown():
    assert "unknown action" in g_action.grounded({"action": "warp"})


def test_action_cross_check_parses_json_array():
    srcs = [{"url": "https://a.com/x"}, {"url": "https://b.com/y"}]
    with mock.patch.object(g, "_search_sources", return_value=srcs):
        out = g_action.grounded({
            "action": "cross_check",
            "claims": '["one", "two"]',
        })
    assert "Cross-check" in out
    assert "one" in out and "two" in out


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
