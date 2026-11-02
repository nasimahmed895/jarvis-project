"""Forecast tracker — immutable write-before-outcome records.

Every strategy decision classified as LONG_BIAS or SHORT_BIAS is recorded
BEFORE its outcome is known. Rows are append-only; evaluation updates a
separate `forecast_outcomes` table so the original forecast is never
rewritten.
"""
from __future__ import annotations

import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..storage.db import (
    connect,
    ensure_schema_version_table,
    get_schema_version,
    set_schema_version,
)

_COMPONENT = "forecast"
_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS forecasts (
    forecast_id TEXT PRIMARY KEY,
    created_at_ms INTEGER NOT NULL,
    asset TEXT NOT NULL,
    source TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    strategy_name TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    direction TEXT NOT NULL,
    reference_price REAL NOT NULL,
    stop_price REAL,
    target_price REAL,
    horizon_bars INTEGER NOT NULL,
    heuristic_confidence REAL NOT NULL,
    indicators_json TEXT NOT NULL,
    cancellation_conditions_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS forecast_outcomes (
    forecast_id TEXT PRIMARY KEY,
    evaluated_at_ms INTEGER NOT NULL,
    terminal_price REAL NOT NULL,
    directionally_correct INTEGER NOT NULL,
    percent_move REAL NOT NULL,
    status TEXT NOT NULL,
    FOREIGN KEY(forecast_id) REFERENCES forecasts(forecast_id)
);
CREATE INDEX IF NOT EXISTS idx_forecasts_asset ON forecasts(asset);
"""


@dataclass(frozen=True)
class ForecastRecord:
    forecast_id: str
    created_at_ms: int
    asset: str
    source: str
    timeframe: str
    strategy_name: str
    strategy_version: str
    direction: str   # "LONG" | "SHORT"
    reference_price: float
    stop_price: float | None
    target_price: float | None
    horizon_bars: int
    heuristic_confidence: float


class ForecastTracker:
    def __init__(self, db_path: str | Path):
        self._conn = connect(db_path)
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

    def record(
        self,
        *,
        asset: str,
        source: str,
        timeframe: str,
        strategy_name: str,
        strategy_version: str,
        direction: str,
        reference_price: float,
        stop_price: float | None,
        target_price: float | None,
        horizon_bars: int,
        heuristic_confidence: float,
        indicators: dict[str, Any],
        cancellation_conditions: list[str],
    ) -> ForecastRecord:
        if direction not in ("LONG", "SHORT"):
            raise ValueError("direction must be LONG or SHORT")
        import json

        fid = f"fcst-{uuid.uuid4().hex[:12]}"
        now = int(time.time() * 1000)
        self._conn.execute(
            """
            INSERT INTO forecasts
                (forecast_id, created_at_ms, asset, source, timeframe,
                 strategy_name, strategy_version, direction, reference_price,
                 stop_price, target_price, horizon_bars,
                 heuristic_confidence, indicators_json,
                 cancellation_conditions_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fid, now, asset, source, timeframe,
                strategy_name, strategy_version, direction, reference_price,
                stop_price, target_price, horizon_bars,
                heuristic_confidence,
                json.dumps(indicators, sort_keys=True),
                json.dumps(list(cancellation_conditions)),
            ),
        )
        return ForecastRecord(
            fid, now, asset, source, timeframe,
            strategy_name, strategy_version, direction, reference_price,
            stop_price, target_price, horizon_bars, heuristic_confidence,
        )

    def evaluate(self, forecast_id: str, terminal_price: float) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT direction, reference_price FROM forecasts WHERE forecast_id = ?",
            (forecast_id,),
        ).fetchone()
        if row is None:
            raise KeyError(forecast_id)
        direction, reference = row
        pct = (terminal_price - reference) / max(abs(reference), 1e-12)
        correct = (direction == "LONG" and pct > 0) or (direction == "SHORT" and pct < 0)
        status = "correct" if correct else "incorrect"
        self._conn.execute(
            """
            INSERT INTO forecast_outcomes
                (forecast_id, evaluated_at_ms, terminal_price,
                 directionally_correct, percent_move, status)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(forecast_id) DO UPDATE SET
                evaluated_at_ms = excluded.evaluated_at_ms,
                terminal_price = excluded.terminal_price,
                directionally_correct = excluded.directionally_correct,
                percent_move = excluded.percent_move,
                status = excluded.status
            """,
            (
                forecast_id,
                int(time.time() * 1000),
                terminal_price,
                1 if correct else 0,
                pct,
                status,
            ),
        )
        return {
            "forecast_id": forecast_id,
            "directionally_correct": correct,
            "percent_move": pct,
            "status": status,
        }

    def accuracy_summary(self, strategy_name: str | None = None) -> dict[str, Any]:
        if strategy_name:
            rows = self._conn.execute(
                """
                SELECT o.directionally_correct FROM forecasts f
                JOIN forecast_outcomes o ON f.forecast_id = o.forecast_id
                WHERE f.strategy_name = ?
                """,
                (strategy_name,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT directionally_correct FROM forecast_outcomes"
            ).fetchall()
        total = len(rows)
        hits = sum(int(r[0]) for r in rows)
        return {
            "total": total,
            "correct": hits,
            "accuracy": (hits / total) if total else None,
        }
