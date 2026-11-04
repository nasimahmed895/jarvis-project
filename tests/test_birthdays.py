"""Tests for actions/birthdays.py."""
from __future__ import annotations

import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import crm
import actions.birthdays as bd


def _fresh():
    crm.connect(Path(tempfile.mktemp(suffix="-bd.sqlite")))
    bd._CONNECTED = True


def test_parse_birthday_variants():
    assert bd._parse_birthday("1990-02-15") == (2, 15)
    assert bd._parse_birthday("02-15") == (2, 15)
    assert bd._parse_birthday("1990/02/15") == (2, 15)
    assert bd._parse_birthday("2/15") == (2, 15)
    assert bd._parse_birthday("") is None
    assert bd._parse_birthday("not a date") is None
    assert bd._parse_birthday("13-40") is None  # invalid month+day


def test_days_until_wraps_to_next_year():
    today = date(2026, 12, 20)
    # Jan 5 is 16 days away from Dec 20 (next year's Jan 5)
    assert bd._days_until(1, 5, today=today) == 16


def test_days_until_feb_29_in_non_leap_year():
    today = date(2026, 1, 1)
    # 2026 is not a leap year — falls back to Feb 28
    assert bd._days_until(2, 29, today=today) == 58


def test_compute_age_when_year_present():
    today = date(2026, 11, 4)
    assert bd._compute_age("1990-12-01") == 36


def test_compute_age_none_when_no_year():
    assert bd._compute_age("12-01") is None


def test_upcoming_filters_by_window():
    _fresh()
    today = date(2026, 11, 4)
    crm.add_contact("Soon", birthday=today.strftime("%Y-%m-%d"))
    crm.add_contact("Within",
                     birthday=(today.replace(month=today.month + 1)
                               if today.month < 12
                               else today.replace(year=today.year + 1,
                                                  month=1)
                               ).strftime("%Y-%m-%d"))
    crm.add_contact("Far", birthday="06-15")  # ~7 months away
    rows = bd.upcoming_birthdays(days=40, today=today)
    names = [r["name"] for r in rows]
    assert "Soon" in names
    assert "Within" in names
    assert "Far" not in names


def test_upcoming_sorts_by_days_until():
    _fresh()
    today = date(2026, 11, 4)
    crm.add_contact("B", birthday="12-01")  # ~27 days
    crm.add_contact("A", birthday="11-10")  # 6 days
    rows = bd.upcoming_birthdays(days=60, today=today)
    assert rows[0]["name"] == "A"
    assert rows[1]["name"] == "B"


def test_action_upcoming_default_horizon():
    _fresh()
    today = date.today()
    # Add a contact with a birthday in ~3 days
    bday = today.replace(year=1990)
    bday_future = (today.toordinal() + 3)
    from datetime import date as _d
    bd_date = _d.fromordinal(bday_future)
    crm.add_contact("TestPerson",
                     birthday=bd_date.strftime("1990-%m-%d"))
    out = bd.birthdays({"action": "upcoming", "days": 30})
    assert "TestPerson" in out
    assert "day(s)" in out


def test_action_rejects_unknown():
    _fresh()
    assert "unknown action" in bd.birthdays({"action": "nope"})


def test_action_today_zero_window():
    _fresh()
    # Nobody with a birthday today
    crm.add_contact("Nope", birthday="01-01")
    out = bd.birthdays({"action": "today"})
    assert "No birthdays today" in out


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
