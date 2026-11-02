"""Persistent store for paper-trading state.

Tables:
    paper_account      one row per account_id with equity + peak + daily P/L
    paper_positions    open simulated positions
    paper_trades       closed simulated trades (immutable append-only log)

Writes happen inside explicit transactions. Reads are isolated per method.
A unique `client_order_id` on `paper_positions` prevents duplicate entries
on restart.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from ..storage.db import (
    connect,
    ensure_schema_version_table,
    get_schema_version,
    set_schema_version,
)

_COMPONENT = "paper"
_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_account (
    account_id TEXT PRIMARY KEY,
    equity_usd REAL NOT NULL,
    cash_usd REAL NOT NULL,
    peak_equity_usd REAL NOT NULL,
    realised_pnl_today_usd REAL NOT NULL,
    pnl_reset_day TEXT NOT NULL,
    updated_at_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_positions (
    client_order_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty REAL NOT NULL,
    entry_price REAL NOT NULL,
    stop_price REAL NOT NULL,
    target_price REAL,
    opened_at_ms INTEGER NOT NULL,
    strategy_name TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    FOREIGN KEY(account_id) REFERENCES paper_account(account_id)
);
CREATE TABLE IF NOT EXISTS paper_trades (
    trade_id TEXT PRIMARY KEY,
    client_order_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty REAL NOT NULL,
    entry_price REAL NOT NULL,
    exit_price REAL NOT NULL,
    opened_at_ms INTEGER NOT NULL,
    closed_at_ms INTEGER NOT NULL,
    gross_pnl_usd REAL NOT NULL,
    fees_usd REAL NOT NULL,
    net_pnl_usd REAL NOT NULL,
    exit_reason TEXT NOT NULL,
    strategy_name TEXT NOT NULL,
    strategy_version TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_paper_positions_account
    ON paper_positions(account_id);
CREATE INDEX IF NOT EXISTS idx_paper_trades_account
    ON paper_trades(account_id);
"""


@dataclass(frozen=True)
class AccountRow:
    account_id: str
    equity_usd: float
    cash_usd: float
    peak_equity_usd: float
    realised_pnl_today_usd: float
    pnl_reset_day: str
    updated_at_ms: int


@dataclass(frozen=True)
class PositionRow:
    client_order_id: str
    account_id: str
    symbol: str
    side: str
    qty: float
    entry_price: float
    stop_price: float
    target_price: float | None
    opened_at_ms: int
    strategy_name: str
    strategy_version: str


@dataclass(frozen=True)
class TradeRow:
    trade_id: str
    client_order_id: str
    account_id: str
    symbol: str
    side: str
    qty: float
    entry_price: float
    exit_price: float
    opened_at_ms: int
    closed_at_ms: int
    gross_pnl_usd: float
    fees_usd: float
    net_pnl_usd: float
    exit_reason: str
    strategy_name: str
    strategy_version: str


class PaperStore:
    def __init__(self, db_path: str | Path):
        self._conn: sqlite3.Connection = connect(db_path)
        self._migrate()

    def close(self) -> None:
        self._conn.close()

    def _migrate(self) -> None:
        ensure_schema_version_table(self._conn)
        if get_schema_version(self._conn, _COMPONENT) < 1:
            for stmt in _SCHEMA.strip().split(";"):
                s = stmt.strip()
                if s:
                    self._conn.execute(s)
            set_schema_version(self._conn, _COMPONENT, 1, int(time.time() * 1000))

    def get_or_init_account(
        self, account_id: str, initial_equity_usd: float, today: str
    ) -> AccountRow:
        row = self._conn.execute(
            "SELECT * FROM paper_account WHERE account_id = ?", (account_id,)
        ).fetchone()
        if row is None:
            now = int(time.time() * 1000)
            self._conn.execute(
                """
                INSERT INTO paper_account
                    (account_id, equity_usd, cash_usd, peak_equity_usd,
                     realised_pnl_today_usd, pnl_reset_day, updated_at_ms)
                VALUES (?, ?, ?, ?, 0.0, ?, ?)
                """,
                (
                    account_id,
                    initial_equity_usd,
                    initial_equity_usd,
                    initial_equity_usd,
                    today,
                    now,
                ),
            )
            return AccountRow(
                account_id=account_id,
                equity_usd=initial_equity_usd,
                cash_usd=initial_equity_usd,
                peak_equity_usd=initial_equity_usd,
                realised_pnl_today_usd=0.0,
                pnl_reset_day=today,
                updated_at_ms=now,
            )
        return AccountRow(*row)

    def update_account(self, acct: AccountRow) -> None:
        self._conn.execute(
            """
            UPDATE paper_account SET
                equity_usd = ?,
                cash_usd = ?,
                peak_equity_usd = ?,
                realised_pnl_today_usd = ?,
                pnl_reset_day = ?,
                updated_at_ms = ?
            WHERE account_id = ?
            """,
            (
                acct.equity_usd,
                acct.cash_usd,
                acct.peak_equity_usd,
                acct.realised_pnl_today_usd,
                acct.pnl_reset_day,
                acct.updated_at_ms,
                acct.account_id,
            ),
        )

    def insert_position(self, pos: PositionRow) -> bool:
        try:
            self._conn.execute(
                """
                INSERT INTO paper_positions
                    (client_order_id, account_id, symbol, side, qty, entry_price,
                     stop_price, target_price, opened_at_ms,
                     strategy_name, strategy_version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pos.client_order_id,
                    pos.account_id,
                    pos.symbol,
                    pos.side,
                    pos.qty,
                    pos.entry_price,
                    pos.stop_price,
                    pos.target_price,
                    pos.opened_at_ms,
                    pos.strategy_name,
                    pos.strategy_version,
                ),
            )
            return True
        except sqlite3.IntegrityError:
            return False

    def remove_position(self, client_order_id: str) -> None:
        self._conn.execute(
            "DELETE FROM paper_positions WHERE client_order_id = ?", (client_order_id,)
        )

    def list_positions(self, account_id: str) -> list[PositionRow]:
        rows = self._conn.execute(
            "SELECT * FROM paper_positions WHERE account_id = ?", (account_id,)
        ).fetchall()
        return [PositionRow(*r) for r in rows]

    def record_trade(self, trade: TradeRow) -> None:
        self._conn.execute(
            """
            INSERT INTO paper_trades
                (trade_id, client_order_id, account_id, symbol, side, qty,
                 entry_price, exit_price, opened_at_ms, closed_at_ms,
                 gross_pnl_usd, fees_usd, net_pnl_usd, exit_reason,
                 strategy_name, strategy_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trade.trade_id,
                trade.client_order_id,
                trade.account_id,
                trade.symbol,
                trade.side,
                trade.qty,
                trade.entry_price,
                trade.exit_price,
                trade.opened_at_ms,
                trade.closed_at_ms,
                trade.gross_pnl_usd,
                trade.fees_usd,
                trade.net_pnl_usd,
                trade.exit_reason,
                trade.strategy_name,
                trade.strategy_version,
            ),
        )

    def list_trades(self, account_id: str) -> list[TradeRow]:
        rows = self._conn.execute(
            "SELECT * FROM paper_trades WHERE account_id = ? ORDER BY closed_at_ms", (account_id,)
        ).fetchall()
        return [TradeRow(*r) for r in rows]
