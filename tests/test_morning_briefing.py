"""Tests for actions/morning_briefing.py — aggregator paths.

We mock out the macOS calendar hook, the weather and news actions, and
Gemini. Core SQL stores (crm/commitments/finance/projects) are pointed
at tempfiles so this doesn't touch the user's data.
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import crm as crm
from core import commitments as com
from core import finance as fin
from core import projects as proj
from core import gemini as gem
import actions.morning_briefing as mb


def _fresh_stores():
    crm.connect(Path(tempfile.mktemp(suffix="-mb-crm.sqlite")))
    com.connect(Path(tempfile.mktemp(suffix="-mb-com.sqlite")))
    fin.connect(Path(tempfile.mktemp(suffix="-mb-fin.sqlite")))
    proj.connect(Path(tempfile.mktemp(suffix="-mb-proj.sqlite")))
    mb._STORES_READY = True


def test_empty_day_message():
    _fresh_stores()
    with mock.patch.object(mb, "_section_schedule", return_value=[]), \
         mock.patch.object(mb, "_section_weather", return_value=[]), \
         mock.patch.object(mb, "_section_news", return_value=[]):
        out = mb.morning_briefing({})
    assert out.startswith("# Morning briefing")
    assert "Looking light" in out


def test_includes_due_commitments():
    _fresh_stores()
    com.add_commitment("ship report", due_ts=time.time() + 3600)
    with mock.patch.object(mb, "_section_schedule", return_value=[]):
        out = mb.morning_briefing({})
    assert "ship report" in out
    assert "## Commitments" in out


def test_includes_follow_ups():
    _fresh_stores()
    cid = crm.add_contact("Jane", email="j@j.com", tier="client")
    crm.add_follow_up("Reply by EOD", contact_id=cid,
                       due_ts=time.time() + 3600)
    with mock.patch.object(mb, "_section_schedule", return_value=[]):
        out = mb.morning_briefing({"follow_hours": 48})
    assert "Follow-ups" in out and "Reply by EOD" in out
    assert "[client]" in out


def test_includes_bills():
    _fresh_stores()
    tomorrow = (datetime.now().date() + timedelta(days=1)).strftime("%Y-%m-%d")
    fin.add_bill("Rent", 1200, tomorrow)
    with mock.patch.object(mb, "_section_schedule", return_value=[]):
        out = mb.morning_briefing({"bills_days": 7})
    assert "Rent" in out and "1,200.00" in out


def test_includes_budget_status():
    _fresh_stores()
    fin.set_budget("food", 100)
    fin.add_expense(80, category="food",
                    date=datetime.now().strftime("%Y-%m-%d"))
    with mock.patch.object(mb, "_section_schedule", return_value=[]):
        out = mb.morning_briefing({})
    assert "Budget status" in out and "food" in out


def test_includes_active_projects_with_pct():
    _fresh_stores()
    pid = proj.add_project("Launch", status="active")
    t = proj.add_project_task(pid, "do it")
    proj.set_project_task_status(t, "done")
    with mock.patch.object(mb, "_section_schedule", return_value=[]):
        out = mb.morning_briefing({})
    assert "Launch" in out
    assert "100%" in out or "100.0%" in out


def test_inbox_section_filters_to_p0_p1():
    _fresh_stores()
    crm.add_contact("CEO", email="ceo@biz.com", tier="vip")
    items = [
        {"subject": "pls look", "sender_email": "ceo@biz.com"},
        {"subject": "lunch?",    "sender_email": "stranger@x.io",
         "body": "nothing special"},
    ]
    with mock.patch.object(mb, "_section_schedule", return_value=[]):
        out = mb.morning_briefing({"inbox_items": json.dumps(items)})
    assert "Priority inbox" in out
    assert "ceo@biz.com" in out
    # P3 unknown sender should NOT appear
    assert "stranger@x.io" not in out


def test_model_intro_prepended_when_use_model():
    _fresh_stores()
    com.add_commitment("ship report", due_ts=time.time() + 3600)
    with mock.patch.object(mb, "_section_schedule", return_value=[]), \
         mock.patch.object(gem, "api_key", return_value="k"), \
         mock.patch.object(gem, "as_json",
                            return_value={"paragraph":
                                          "Good morning — one critical item."}):
        out = mb.morning_briefing({"use_model": "1"})
    assert "Good morning" in out


def test_model_intro_skipped_without_key():
    _fresh_stores()
    com.add_commitment("ship report", due_ts=time.time() + 3600)
    with mock.patch.object(mb, "_section_schedule", return_value=[]), \
         mock.patch.object(gem, "api_key", return_value=""):
        out = mb.morning_briefing({"use_model": "1"})
    assert "Good morning" not in out
    assert "## Commitments" in out


def test_news_section_renders_hits():
    _fresh_stores()
    fake_news = [
        {"title": "AI news A", "url": "https://x", "source": "X"},
        {"title": "AI news B", "url": "https://y", "source": "Y"},
    ]
    with mock.patch.object(mb, "_section_schedule", return_value=[]), \
         mock.patch("actions.web_search._news", return_value=fake_news,
                     create=True):
        out = mb.morning_briefing({"news_query": "AI"})
    assert "## News — AI" in out
    assert "AI news A" in out


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
