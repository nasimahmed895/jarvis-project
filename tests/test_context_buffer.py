"""Tests for core/context_buffer.py + actions/context_recall.py."""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import context_buffer as cb
import actions.context_recall as cr


def _reset():
    cb.clear()


def test_record_then_recent_newest_first():
    _reset()
    cb.record("voice", "hello")
    cb.record("vision:screen:ui", "mail app visible")
    rows = cb.recent(limit=5)
    assert rows[0]["summary"] == "mail app visible"
    assert rows[1]["summary"] == "hello"


def test_recent_filters_by_kind_prefix():
    _reset()
    cb.record("voice", "hi")
    cb.record("vision:screen:ui", "mail app")
    cb.record("vision:camera:describe", "dog on sofa")
    rows = cb.recent(limit=5, kinds=["vision"])
    assert len(rows) == 2
    assert all(e["kind"].startswith("vision") for e in rows)


def test_recent_filters_by_since_seconds():
    _reset()
    now = time.time()
    cb.record("a", "old", ts=now - 120)
    cb.record("a", "new", ts=now - 5)
    rows = cb.recent(since_seconds=30)
    summaries = [r["summary"] for r in rows]
    assert "new" in summaries and "old" not in summaries


def test_search_substring_case_insensitive():
    _reset()
    cb.record("vision", "Red Toyota Corolla in driveway")
    cb.record("voice", "lunch at noon")
    hits = cb.search("toyota")
    assert len(hits) == 1
    assert "Toyota" in hits[0]["summary"]


def test_ring_buffer_caps_at_max():
    _reset()
    for i in range(cb._MAX_ITEMS + 10):
        cb.record("spam", f"item {i}")
    assert cb.size() == cb._MAX_ITEMS
    # Oldest should have been evicted — so "item 0" is gone and
    # "item (max+9)" is still there.
    rows = cb.recent(limit=cb._MAX_ITEMS)
    summaries = [r["summary"] for r in rows]
    assert f"item {cb._MAX_ITEMS + 9}" in summaries
    assert "item 0" not in summaries


def test_action_recent_and_record():
    _reset()
    out = cr.context_recall({"action": "record",
                              "kind": "note",
                              "summary": "red ball"})
    assert "Recorded" in out
    out = cr.context_recall({"action": "recent"})
    assert "red ball" in out


def test_action_search():
    _reset()
    cb.record("vision", "Red Toyota in driveway")
    out = cr.context_recall({"action": "search", "query": "toyota"})
    assert "Toyota" in out


def test_action_size_and_clear():
    _reset()
    cb.record("x", "a")
    cb.record("x", "b")
    out = cr.context_recall({"action": "size"})
    assert "2 entries" in out
    cr.context_recall({"action": "clear"})
    assert cb.size() == 0


def test_action_rejects_unknown():
    assert "unknown action" in cr.context_recall({"action": "warp"})


def test_record_never_raises_on_bad_input():
    # Should not raise, just store best-effort.
    cb.record(None, None)  # type: ignore[arg-type]
    cb.record(123, {"bad": "object"})  # type: ignore[arg-type]
    assert cb.size() >= 1


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
