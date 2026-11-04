"""Tests for actions/meeting_prep.py.

Calendar access is macOS-only; we exercise the deterministic 'brief'
path with an explicit event and check that the aggregator pulls the
right fields out of CRM, commitments, and follow-ups.
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import crm
from core import commitments as com
import actions.meeting_prep as mp


def _fresh_stores() -> None:
    crm.connect(Path(tempfile.mktemp(suffix="-mp-crm.sqlite")))
    com.connect(Path(tempfile.mktemp(suffix="-mp-com.sqlite")))
    mp._CRM_CONNECTED = True
    mp._COM_CONNECTED = True


def test_parse_attendees_mixed():
    out = mp._parse_attendees("Jane <jane@acme.com>, Bob")
    names = [a["name"] for a in out]
    emails = [a["email"] for a in out]
    assert "Jane" in names and "Bob" in names
    assert "jane@acme.com" in emails


def test_parse_attendees_newline_separated():
    out = mp._parse_attendees("alice@a.com\nBob\nceo@biz.com")
    assert len(out) == 3


def test_brief_shows_crm_tier_and_recent_interaction():
    _fresh_stores()
    cid = crm.add_contact("Jane Doe", email="jane@acme.com", tier="client",
                          org="Acme", role="CTO")
    crm.log_interaction(cid, "email_in", subject="RFP kickoff",
                        summary="sent brief", ts=time.time() - 86400)
    with mock.patch.object(mp, "_collect_events", return_value=[]):
        out = mp.meeting_prep({
            "action": "brief",
            "title":  "Acme sync",
            "start":  "2099-01-05 10:00",
            "end":    "2099-01-05 11:00",
            "attendees": "jane@acme.com",
        })
    assert "Acme sync" in out
    assert "[client]" in out
    assert "Jane Doe" in out
    assert "CTO" in out
    assert "RFP kickoff" in out


def test_brief_includes_open_follow_up_for_attendee():
    _fresh_stores()
    cid = crm.add_contact("Omar", email="omar@biz.com", tier="investor")
    crm.add_follow_up("Reply to Omar's email", contact_id=cid,
                      due_ts=time.time() + 86400)
    with mock.patch.object(mp, "_collect_events", return_value=[]):
        out = mp.meeting_prep({
            "action": "brief",
            "title":  "Investor check-in",
            "start":  "2099-01-05 10:00",
            "attendees": "omar@biz.com",
        })
    assert "Reply to Omar's email" in out
    assert "Open follow-ups" in out


def test_brief_includes_commitment_mentioning_attendee():
    _fresh_stores()
    crm.add_contact("Sarah", email="sarah@c.co", tier="vip")
    com.add_commitment("Send Sarah the deck by Friday",
                        due_ts=time.time() + 86400 * 3)
    with mock.patch.object(mp, "_collect_events", return_value=[]):
        out = mp.meeting_prep({
            "action": "brief",
            "title":  "1:1 w Sarah",
            "start":  "2099-01-05 10:00",
            "attendees": "Sarah <sarah@c.co>",
        })
    assert "Send Sarah the deck" in out
    assert "Your commitments" in out


def test_brief_flags_calendar_conflict():
    _fresh_stores()
    conflict_event = {
        "start": "2099-01-05 10:30",
        "end":   "2099-01-05 11:30",
        "title": "Standup",
    }
    with mock.patch.object(mp, "_collect_events",
                           return_value=[conflict_event]):
        out = mp.meeting_prep({
            "action": "brief",
            "title":  "Pricing review",
            "start":  "2099-01-05 10:00",
            "end":    "2099-01-05 11:00",
        })
    assert "Calendar conflicts" in out
    assert "Standup" in out


def test_brief_handles_unknown_attendee_gracefully():
    _fresh_stores()
    with mock.patch.object(mp, "_collect_events", return_value=[]):
        out = mp.meeting_prep({
            "action": "brief",
            "title":  "intro",
            "start":  "2099-01-05 10:00",
            "attendees": "stranger@nowhere.io",
        })
    assert "not in CRM" in out


def test_action_requires_title_and_start():
    _fresh_stores()
    assert "'title'" in mp.meeting_prep({"action": "brief",
                                         "start": "2099-01-05 10:00"})
    assert "'title'" in mp.meeting_prep({"action": "brief",
                                         "title": "x"})


def test_action_unknown():
    _fresh_stores()
    assert "unknown action" in mp.meeting_prep({"action": "foobar"})


def test_upcoming_empty_on_non_macos():
    _fresh_stores()
    with mock.patch.object(mp, "_collect_events", return_value=[]):
        out = mp.meeting_prep({"action": "upcoming"})
    assert "No upcoming" in out


def test_find_next_event_selects_future():
    from datetime import datetime
    events = [
        {"start": "2025-01-01 09:00", "title": "past"},
        {"start": "2099-02-02 10:00", "title": "future"},
    ]
    # Patch "now" so the test is deterministic.
    ev = mp._find_next_event(events, now=datetime(2026, 11, 4))
    assert ev and ev["title"] == "future"


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
