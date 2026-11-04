"""Tests for core/travel.py + actions/travel.py."""
from __future__ import annotations

import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import travel as tv
import actions.travel as tv_action


def _fresh():
    tv.connect(Path(tempfile.mktemp(suffix="-tv.sqlite")))
    tv_action._CONNECTED = True


def test_add_and_get_trip():
    _fresh()
    tid = tv.add_trip("Tokyo Oct 2027", destination="Tokyo",
                      start_date="2027-10-10", end_date="2027-10-18",
                      status="booked")
    t = tv.get_trip(tid)
    assert t and t["destination"] == "Tokyo" and t["status"] == "booked"


def test_add_rejects_empty_name():
    _fresh()
    try:
        tv.add_trip("")
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_status_normalisation():
    _fresh()
    tid = tv.add_trip("x", status="weird")  # falls back
    assert tv.get_trip(tid)["status"] == "planning"


def test_add_leg_defaults_and_cost_currency():
    _fresh()
    tid = tv.add_trip("trip", status="booked")
    lid = tv.add_leg(tid, kind="flight", carrier="JL",
                     from_loc="NRT", to_loc="DXB",
                     start_ts="2027-10-10 18:00",
                     cost=1200, currency="usd")
    legs = tv.list_legs(tid)
    assert legs and legs[0]["id"] == lid
    assert legs[0]["cost"] == 1200.0
    assert legs[0]["currency"] == "USD"


def test_add_leg_kind_normalised():
    _fresh()
    tid = tv.add_trip("t")
    lid = tv.add_leg(tid, kind="SPACESHIP")
    assert tv.list_legs(tid)[0]["kind"] == "note"


def test_update_leg_cost_round_trip():
    _fresh()
    tid = tv.add_trip("t")
    lid = tv.add_leg(tid, kind="hotel", cost=250)
    assert tv.update_leg(lid, cost=300, currency="eur")
    legs = tv.list_legs(tid)
    assert legs[0]["cost"] == 300.0
    assert legs[0]["currency"] == "EUR"


def test_total_cost_groups_by_currency():
    _fresh()
    tid = tv.add_trip("t")
    tv.add_leg(tid, kind="flight", cost=800, currency="USD")
    tv.add_leg(tid, kind="hotel", cost=600, currency="USD")
    tv.add_leg(tid, kind="activity", cost=150, currency="EUR")
    tv.add_leg(tid, kind="note", notes="no cost")
    totals = tv.trip_total_cost(tid)
    assert totals == {"USD": 1400.0, "EUR": 150.0}


def test_next_leg_returns_future_only():
    _fresh()
    tid = tv.add_trip("t")
    past = time.time() - 86400
    future = time.time() + 86400
    tv.add_leg(tid, kind="flight", start_ts=past)
    tv.add_leg(tid, kind="flight", start_ts=future)
    nxt = tv.next_leg(trip_id=tid)
    assert nxt and nxt["start_ts"] >= time.time()


def test_next_leg_across_all_trips():
    _fresh()
    t1 = tv.add_trip("a")
    t2 = tv.add_trip("b")
    tv.add_leg(t1, kind="hotel", start_ts=time.time() + 100)
    tv.add_leg(t2, kind="flight", start_ts=time.time() + 50)
    nxt = tv.next_leg()
    assert nxt and nxt["trip_id"] == t2


def test_upcoming_trips_includes_in_progress():
    _fresh()
    today = datetime.now().date()
    yday = (today - timedelta(days=1)).strftime("%Y-%m-%d")
    tmrw = (today + timedelta(days=1)).strftime("%Y-%m-%d")
    ongoing = tv.add_trip("ongoing", start_date=yday, end_date=tmrw,
                           status="in_progress")
    future_id = tv.add_trip("future",
                              start_date=(today + timedelta(days=5))
                              .strftime("%Y-%m-%d"),
                              status="booked")
    past_id = tv.add_trip("past",
                            end_date=(today - timedelta(days=10))
                            .strftime("%Y-%m-%d"),
                            status="done")
    rows = tv.upcoming_trips(within_days=30)
    ids = {r["id"] for r in rows}
    assert ongoing in ids and future_id in ids
    assert past_id not in ids


def test_cancel_trip_marks_cancelled():
    _fresh()
    tid = tv.add_trip("to be cancelled", status="booked")
    assert tv.cancel_trip(tid)
    assert tv.get_trip(tid)["status"] == "cancelled"


def test_delete_leg():
    _fresh()
    tid = tv.add_trip("t")
    lid = tv.add_leg(tid, kind="transfer")
    assert tv.delete_leg(lid)
    assert tv.list_legs(tid) == []


# ── Action layer ─────────────────────────────────────────────────────────────

def test_action_add_trip_and_leg():
    _fresh()
    out = tv_action.travel({"action": "add_trip", "name": "Paris"})
    assert "Trip #" in out
    # Pull id from list
    trip = tv.list_trips()[0]
    out = tv_action.travel({
        "action": "add_leg", "trip_id": trip["id"],
        "kind": "flight", "from_loc": "SFO", "to_loc": "CDG",
        "start": "2027-04-10 11:00", "cost": 900, "currency": "usd",
    })
    assert "Added leg" in out
    out = tv_action.travel({"action": "get_trip", "id": trip["id"]})
    assert "SFO" in out and "CDG" in out


def test_action_total_cost_output():
    _fresh()
    tid = tv.add_trip("t")
    tv.add_leg(tid, kind="hotel", cost=200, currency="USD")
    out = tv_action.travel({"action": "total_cost", "trip_id": tid})
    assert "USD" in out and "200" in out


def test_action_rejects_unknown():
    _fresh()
    assert "unknown action" in tv_action.travel({"action": "warp"})


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
