"""Tests for core/session_store.py."""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import session_store as ss


def _tmp() -> Path:
    return Path(tempfile.mktemp(suffix="-ss.json"))


def test_save_and_load_round_trip():
    p = _tmp()
    assert ss.save_handle("abc123", path=p)
    assert ss.load_handle(path=p) == "abc123"


def test_load_returns_none_for_missing_file():
    p = _tmp()
    assert ss.load_handle(path=p) is None


def test_load_rejects_stale_handle():
    p = _tmp()
    ss.save_handle("old", path=p)
    # Fake a future timestamp beyond TTL
    now = time.time() + ss.DEFAULT_TTL_SECONDS + 100
    assert ss.load_handle(path=p, now=now) is None
    # Stale load must have deleted the file so the stale state doesn't
    # linger on disk forever.
    assert not p.exists()


def test_load_honours_custom_ttl():
    p = _tmp()
    ss.save_handle("x", path=p)
    assert ss.load_handle(path=p, ttl_seconds=0.001,
                            now=time.time() + 10) is None


def test_clear_removes_file():
    p = _tmp()
    ss.save_handle("k", path=p)
    ss.clear_handle(path=p)
    assert not p.exists()


def test_load_rejects_malformed_json():
    p = _tmp()
    p.write_text("not json at all")
    assert ss.load_handle(path=p) is None


def test_load_rejects_missing_fields():
    p = _tmp()
    p.write_text('{"handle": "x"}')  # no updated_at
    assert ss.load_handle(path=p) is None


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
