"""SQLite connection helper for the market package.

Phase 2 provides the connect() helper only. Table creation lives next to the
subsystem that owns the schema (candles cache, backtest runs, paper trades,
forecasts) and is added at the phase that introduces that subsystem.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from ..exceptions import MarketError


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open (or create) a SQLite database with safe pragmas.

    - foreign_keys=ON so later schemas can rely on referential integrity.
    - journal_mode=WAL for concurrent readers while a writer is active.
    - busy_timeout=5000 ms so a transient lock does not become a crash.
    """
    p = Path(db_path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise MarketError(f"cannot create parent dir for {p}: {e}") from None
    try:
        conn = sqlite3.connect(str(p), timeout=10.0, isolation_level=None)
    except sqlite3.Error as e:
        raise MarketError(f"cannot open database {p}: {e}") from None
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def ensure_schema_version_table(conn: sqlite3.Connection) -> None:
    """Create the schema-version table if it does not yet exist.

    Each subsystem that owns a table calls this and inserts its own
    (component, version) row the first time it runs its migration. The table
    is tolerant of multiple components registering independently.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            component TEXT PRIMARY KEY,
            version INTEGER NOT NULL,
            applied_at_ms INTEGER NOT NULL
        )
        """
    )


def get_schema_version(conn: sqlite3.Connection, component: str) -> int:
    row = conn.execute(
        "SELECT version FROM schema_version WHERE component = ?", (component,)
    ).fetchone()
    return int(row[0]) if row else 0


def set_schema_version(conn: sqlite3.Connection, component: str, version: int, now_ms: int) -> None:
    conn.execute(
        """
        INSERT INTO schema_version (component, version, applied_at_ms)
        VALUES (?, ?, ?)
        ON CONFLICT(component) DO UPDATE SET version = excluded.version, applied_at_ms = excluded.applied_at_ms
        """,
        (component, int(version), int(now_ms)),
    )
