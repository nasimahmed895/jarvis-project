"""SQLite OHLCV cache.

One table per installation holds every candle ever retrieved, keyed by
(provider, symbol, timeframe, open_time_ms). Writes are idempotent — the same
candle re-fetched after a retransmission overwrites in place rather than
appending.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Iterable, Sequence

from ..storage.db import (
    connect,
    ensure_schema_version_table,
    get_schema_version,
    set_schema_version,
)
from ..types import Candle, OHLCVFrame, Timeframe

_COMPONENT = "candles"
_LATEST_VERSION = 1


_SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
    provider TEXT NOT NULL,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    open_time_ms INTEGER NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL NOT NULL,
    close_time_ms INTEGER NOT NULL,
    is_closed INTEGER NOT NULL,
    PRIMARY KEY (provider, symbol, timeframe, open_time_ms)
);
CREATE INDEX IF NOT EXISTS idx_candles_lookup
    ON candles (provider, symbol, timeframe, open_time_ms DESC);
"""


class CandleCache:
    def __init__(self, db_path: str | Path):
        self._conn: sqlite3.Connection = connect(db_path)
        self._migrate()

    def close(self) -> None:
        self._conn.close()

    def _migrate(self) -> None:
        ensure_schema_version_table(self._conn)
        v = get_schema_version(self._conn, _COMPONENT)
        if v < 1:
            for stmt in _SCHEMA.strip().split(";"):
                s = stmt.strip()
                if s:
                    self._conn.execute(s)
            set_schema_version(self._conn, _COMPONENT, 1, int(time.time() * 1000))
        # Future migrations: `if v < 2: ...`.

    # ── writes ───────────────────────────────────────────────────────────
    def upsert_many(
        self,
        provider: str,
        symbol: str,
        timeframe: Timeframe,
        candles: Iterable[Candle],
    ) -> int:
        rows = [
            (
                provider,
                symbol,
                timeframe.value,
                int(c.open_time_ms),
                float(c.open),
                float(c.high),
                float(c.low),
                float(c.close),
                float(c.volume),
                int(c.close_time_ms),
                1 if c.is_closed else 0,
            )
            for c in candles
        ]
        if not rows:
            return 0
        self._conn.executemany(
            """
            INSERT INTO candles
                (provider, symbol, timeframe, open_time_ms,
                 open, high, low, close, volume, close_time_ms, is_closed)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider, symbol, timeframe, open_time_ms) DO UPDATE SET
                open = excluded.open,
                high = excluded.high,
                low = excluded.low,
                close = excluded.close,
                volume = excluded.volume,
                close_time_ms = excluded.close_time_ms,
                is_closed = excluded.is_closed
            """,
            rows,
        )
        return len(rows)

    def store_frame(self, frame: OHLCVFrame) -> int:
        return self.upsert_many(frame.source, frame.symbol, frame.timeframe, frame.candles)

    # ── reads ────────────────────────────────────────────────────────────
    def latest_candle(
        self, provider: str, symbol: str, timeframe: Timeframe
    ) -> Candle | None:
        row = self._conn.execute(
            """
            SELECT open_time_ms, open, high, low, close, volume, close_time_ms, is_closed
            FROM candles
            WHERE provider = ? AND symbol = ? AND timeframe = ?
            ORDER BY open_time_ms DESC LIMIT 1
            """,
            (provider, symbol, timeframe.value),
        ).fetchone()
        if row is None:
            return None
        return _row_to_candle(row)

    def load_frame(
        self,
        provider: str,
        symbol: str,
        timeframe: Timeframe,
        limit: int,
        end_ts_ms: int | None = None,
    ) -> OHLCVFrame:
        params: list = [provider, symbol, timeframe.value]
        extra = ""
        if end_ts_ms is not None:
            extra = "AND open_time_ms <= ?"
            params.append(int(end_ts_ms))
        rows = self._conn.execute(
            f"""
            SELECT open_time_ms, open, high, low, close, volume, close_time_ms, is_closed
            FROM candles
            WHERE provider = ? AND symbol = ? AND timeframe = ? {extra}
            ORDER BY open_time_ms DESC LIMIT ?
            """,
            (*params, int(limit)),
        ).fetchall()
        rows.reverse()
        candles = tuple(_row_to_candle(r) for r in rows)
        return OHLCVFrame(
            symbol=symbol,
            timeframe=timeframe,
            source=provider,
            retrieved_at_ms=0,  # unknown — cache does not stamp retrieval.
            candles=candles,
        )


def _row_to_candle(row: Sequence) -> Candle:
    (open_ms, o, h, l, c, v, close_ms, is_closed) = row
    return Candle(
        open_time_ms=int(open_ms),
        open=float(o),
        high=float(h),
        low=float(l),
        close=float(c),
        volume=float(v),
        close_time_ms=int(close_ms),
        is_closed=bool(is_closed),
    )
