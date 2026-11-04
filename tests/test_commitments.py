"""Tests for core/commitments.py + actions/commitments.py."""
from __future__ import annotations

import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import commitments as com
import actions.commitments as com_action


def _fresh_db() -> Path:
    p = Path(tempfile.mktemp(suffix="-com.sqlite"))
    com.connect(p)
    com_action._CONNECTED = True
    return p


# ── CRUD ─────────────────────────────────────────────────────────────────────

def test_add_and_list_open():
    _fresh_db()
    cid = com.add_commitment("send proposal to Acme")
    rows = com.list_open()
    assert rows and rows[0]["id"] == cid
    assert rows[0]["status"] == "open"


def test_add_dedupes_on_normalised_text():
    _fresh_db()
    a = com.add_commitment("Call Sarah")
    b = com.add_commitment("call sarah")
    c = com.add_commitment("  call   sarah  ")
    assert a == b == c
    assert len(com.list_open()) == 1


def test_add_keeps_earlier_due():
    _fresh_db()
    late = time.time() + 7 * 86400
    early = time.time() + 1 * 86400
    a = com.add_commitment("ship report", due_ts=late)
    b = com.add_commitment("ship report", due_ts=early)
    assert a == b
    row = com.list_open()[0]
    assert abs(row["due_ts"] - early) < 1.0


def test_resolve_and_miss_require_open():
    _fresh_db()
    cid = com.add_commitment("do thing")
    assert com.resolve(cid)
    assert not com.resolve(cid)       # already done, no second resolve
    assert not com.miss(cid)          # not open anymore


def test_snooze_pushes_due():
    _fresh_db()
    cid = com.add_commitment("ping Omar", due_ts=time.time())
    assert com.snooze(cid, by_hours=48)
    row = com.list_open()[0]
    # Should be ~48h out.
    assert (row["due_ts"] - time.time()) > 47 * 3600


def test_cancel_closes_row():
    _fresh_db()
    cid = com.add_commitment("x")
    assert com.cancel(cid)
    assert cid not in [r["id"] for r in com.list_open()]


def test_due_within_filters():
    _fresh_db()
    com.add_commitment("tomorrow thing", due_ts=time.time() + 12 * 3600)
    com.add_commitment("next week thing", due_ts=time.time() + 8 * 86400)
    within_24 = com.due_within(24)
    assert len(within_24) == 1
    assert within_24[0]["text"] == "tomorrow thing"


def test_sweep_missed_marks_stale_open():
    _fresh_db()
    cid = com.add_commitment("ancient promise",
                             due_ts=time.time() - 3 * 86400)
    fresh = com.add_commitment("fresh promise",
                               due_ts=time.time() + 3600)
    n = com.sweep_missed(grace_hours=12)
    assert n == 1
    missed = [r for r in com.list_all(status="missed")]
    assert any(r["id"] == cid for r in missed)
    assert fresh in [r["id"] for r in com.list_open()]


# ── Extractor ────────────────────────────────────────────────────────────────

def test_extract_simple_first_person():
    found = com.extract_from_text("I will send the proposal tomorrow.")
    assert len(found) == 1
    assert "send the proposal" in found[0].text
    assert found[0].due_text and "tomorrow" in found[0].due_text
    assert found[0].due_ts is not None


def test_extract_ignores_non_commitments():
    found = com.extract_from_text(
        "She will send the proposal tomorrow. "
        "He promised to call. "
        "The weather will change."
    )
    # Only first-person commitments count.
    assert found == []


def test_extract_handles_multiple_in_one_utterance():
    text = ("I'll book the flight by Friday, and I need to "
            "review the deck tonight.")
    found = com.extract_from_text(text)
    assert len(found) == 2
    texts = [e.text for e in found]
    assert any("book the flight" in t for t in texts)
    assert any("review the deck" in t for t in texts)


def test_extract_weekday_resolves_to_future():
    # Fixed baseline to make the test deterministic.
    base = datetime(2026, 11, 4, 10, 0, 0)  # Wednesday
    found = com.extract_from_text("I'll ping Omar on Friday.", now=base)
    assert len(found) == 1
    assert found[0].due_ts is not None
    assert datetime.fromtimestamp(found[0].due_ts) > base


def test_extract_end_of_week():
    base = datetime(2026, 11, 4, 10, 0, 0)  # Wednesday
    found = com.extract_from_text("I'll ship it by end of week.", now=base)
    assert len(found) == 1
    assert found[0].due_ts is not None
    assert found[0].due_text and "end of week" in found[0].due_text.lower()


def test_extract_and_store_round_trip():
    _fresh_db()
    text = "I'll call Jane tomorrow."
    ids = com.extract_and_store(text)
    assert len(ids) == 1
    rows = com.list_open()
    assert rows and "call Jane" in rows[0]["text"]
    assert rows[0]["source"] == "extracted"


# ── Action layer ─────────────────────────────────────────────────────────────

def test_action_add_and_list():
    _fresh_db()
    out = com_action.commitments({"action": "add", "text": "pay rent"})
    assert "Logged commitment" in out
    out = com_action.commitments({"action": "list"})
    assert "pay rent" in out


def test_action_extract_invocation():
    _fresh_db()
    out = com_action.commitments({
        "action": "extract",
        "text": "I'll follow up with the vendor by Friday.",
    })
    assert "Extracted" in out
    assert "follow up" in out


def test_action_resolve_round_trip():
    _fresh_db()
    com.add_commitment("test item")
    cid = com.list_open()[0]["id"]
    out = com_action.commitments({"action": "resolve", "id": cid})
    assert "Resolved" in out
    assert com.list_open() == []


def test_action_due_with_natural_date():
    _fresh_db()
    # Add via action with natural-language due.
    com_action.commitments({
        "action": "add",
        "text": "sanity check",
        "due": "tomorrow",
    })
    out = com_action.commitments({"action": "due", "hours": 48})
    assert "sanity check" in out


def test_action_rejects_bad_id():
    _fresh_db()
    assert "provide numeric" in com_action.commitments(
        {"action": "resolve", "id": "not a number"}
    )


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
