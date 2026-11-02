"""Phase 2 — SQLite storage helper tests."""
from __future__ import annotations

from pathlib import Path

from market.storage import db as sdb


def test_connect_creates_db(tmp_path: Path) -> None:
    p = tmp_path / "a.db"
    conn = sdb.connect(p)
    try:
        cur = conn.execute("PRAGMA journal_mode")
        assert cur.fetchone()[0] in ("wal", "memory")
        cur = conn.execute("PRAGMA foreign_keys")
        assert cur.fetchone()[0] == 1
    finally:
        conn.close()


def test_schema_version_roundtrip(tmp_path: Path) -> None:
    conn = sdb.connect(tmp_path / "b.db")
    try:
        sdb.ensure_schema_version_table(conn)
        assert sdb.get_schema_version(conn, "candles") == 0
        sdb.set_schema_version(conn, "candles", 1, 1_700_000_000_000)
        assert sdb.get_schema_version(conn, "candles") == 1
        sdb.set_schema_version(conn, "candles", 2, 1_700_000_010_000)
        assert sdb.get_schema_version(conn, "candles") == 2
    finally:
        conn.close()
