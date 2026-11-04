"""
Travel itinerary store.

Fills section 8 beyond the browser-scraped flight finder: an on-disk
store for the user's trips, their legs (flights, hotels, transfers,
activities), and a next-action query so JARVIS can say "your flight to
Tokyo leaves in 6 hours, gate C14".

Writes are local. Nothing books, nothing pays. Pairs cleanly with
`actions/flight_finder.py` for research and `core/finance.py` for
expense tracking (each leg has a cost; the voice loop can push it into
finance on confirmation).
"""
from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

_LOCK = threading.RLock()
_DB: sqlite3.Connection | None = None
_DB_PATH: Path | None = None


TRIP_STATUSES = ("planning", "booked", "in_progress", "done", "cancelled")
LEG_KINDS = ("flight", "train", "bus", "car_rental", "hotel",
             "transfer", "activity", "note")


def connect(db_path: Path | str) -> None:
    global _DB, _DB_PATH
    with _LOCK:
        want = Path(db_path)
        if _DB is not None and _DB_PATH == want:
            return
        if _DB is not None:
            try:
                _DB.close()
            except Exception:
                pass
        want.parent.mkdir(parents=True, exist_ok=True)
        _DB = sqlite3.connect(str(want), check_same_thread=False)
        _DB.row_factory = sqlite3.Row
        _DB.execute("PRAGMA journal_mode=WAL")
        _DB.execute("PRAGMA foreign_keys=ON")
        _DB_PATH = want
        _init_schema()


def _init_schema() -> None:
    assert _DB is not None
    _DB.executescript(
        """
        CREATE TABLE IF NOT EXISTS trips (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            name         TEXT    NOT NULL,
            destination  TEXT    DEFAULT NULL,
            start_date   TEXT    DEFAULT NULL,
            end_date     TEXT    DEFAULT NULL,
            status       TEXT    NOT NULL DEFAULT 'planning',
            notes        TEXT    DEFAULT NULL,
            created_at   REAL    NOT NULL,
            updated_at   REAL    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_trips_start ON trips(start_date);

        CREATE TABLE IF NOT EXISTS trip_legs (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            trip_id      INTEGER NOT NULL,
            kind         TEXT    NOT NULL DEFAULT 'note',
            carrier      TEXT    DEFAULT NULL,       -- airline / hotel brand / etc
            reference    TEXT    DEFAULT NULL,       -- confirmation code
            from_loc     TEXT    DEFAULT NULL,
            to_loc       TEXT    DEFAULT NULL,
            start_ts     REAL    DEFAULT NULL,
            end_ts       REAL    DEFAULT NULL,
            cost         REAL    DEFAULT NULL,
            currency     TEXT    DEFAULT NULL,
            notes        TEXT    DEFAULT NULL,
            created_at   REAL    NOT NULL,
            FOREIGN KEY (trip_id) REFERENCES trips(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_legs_trip  ON trip_legs(trip_id);
        CREATE INDEX IF NOT EXISTS idx_legs_start ON trip_legs(start_ts);
        """
    )
    _DB.commit()


def _now() -> float:
    return time.time()


def _norm_status(raw: str | None) -> str:
    v = (raw or "").strip().lower()
    return v if v in TRIP_STATUSES else "planning"


def _norm_kind(raw: str | None) -> str:
    v = (raw or "").strip().lower()
    return v if v in LEG_KINDS else "note"


def _parse_ts(raw) -> float | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw).strip()
    try:
        return float(s)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).timestamp()
        except ValueError:
            continue
    return None


# ── Trips ───────────────────────────────────────────────────────────────────

def add_trip(name: str, *, destination: str | None = None,
             start_date: str | None = None, end_date: str | None = None,
             status: str = "planning", notes: str | None = None) -> int:
    if not name.strip():
        raise ValueError("trip name required")
    now = _now()
    with _LOCK:
        cur = _DB.execute(
            "INSERT INTO trips(name, destination, start_date, end_date, "
            "status, notes, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (name.strip(), destination, start_date, end_date,
             _norm_status(status), notes, now, now),
        )
        _DB.commit()
        return int(cur.lastrowid)


def update_trip(trip_id: int, **fields) -> bool:
    allowed = {"name", "destination", "start_date", "end_date",
               "status", "notes"}
    sets: list[str] = []
    vals: list = []
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k == "status":
            v = _norm_status(v)
        sets.append(f"{k}=?")
        vals.append(v)
    if not sets:
        return False
    sets.append("updated_at=?")
    vals.append(_now())
    vals.append(trip_id)
    with _LOCK:
        res = _DB.execute(
            f"UPDATE trips SET {', '.join(sets)} WHERE id=?", vals
        )
        _DB.commit()
    return bool(res.rowcount)


def cancel_trip(trip_id: int) -> bool:
    return update_trip(trip_id, status="cancelled")


def get_trip(trip_id: int) -> dict | None:
    with _LOCK:
        row = _DB.execute(
            "SELECT * FROM trips WHERE id=?", (trip_id,)
        ).fetchone()
    return dict(row) if row else None


def list_trips(status: str | Iterable[str] | None = None,
               limit: int = 50) -> list[dict]:
    with _LOCK:
        if status is None:
            rows = _DB.execute(
                "SELECT * FROM trips ORDER BY start_date LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            if isinstance(status, str):
                status = [status]
            vals = [_norm_status(s) for s in status]
            ph = ",".join("?" * len(vals))
            rows = _DB.execute(
                f"SELECT * FROM trips WHERE status IN ({ph}) "
                f"ORDER BY start_date LIMIT ?",
                (*vals, limit),
            ).fetchall()
    return [dict(r) for r in rows]


def upcoming_trips(within_days: int = 90,
                    now: datetime | None = None) -> list[dict]:
    now = now or datetime.now()
    horizon = (now + timedelta(days=within_days)).strftime("%Y-%m-%d")
    today = now.strftime("%Y-%m-%d")
    with _LOCK:
        rows = _DB.execute(
            "SELECT * FROM trips WHERE "
            "status IN ('planning','booked','in_progress') AND "
            "(start_date IS NULL OR "
            " (start_date<=? AND (end_date IS NULL OR end_date>=?)) OR "
            " (start_date>=? AND start_date<=?)) "
            "ORDER BY start_date",
            (today, today, today, horizon),
        ).fetchall()
    return [dict(r) for r in rows]


# ── Legs ────────────────────────────────────────────────────────────────────

def add_leg(trip_id: int, *, kind: str = "note",
            carrier: str | None = None, reference: str | None = None,
            from_loc: str | None = None, to_loc: str | None = None,
            start_ts=None, end_ts=None,
            cost: float | None = None, currency: str | None = None,
            notes: str | None = None) -> int:
    with _LOCK:
        cur = _DB.execute(
            "INSERT INTO trip_legs(trip_id, kind, carrier, reference, "
            "from_loc, to_loc, start_ts, end_ts, cost, currency, notes, "
            "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (trip_id, _norm_kind(kind), carrier, reference,
             from_loc, to_loc,
             _parse_ts(start_ts), _parse_ts(end_ts),
             float(cost) if cost is not None and cost != "" else None,
             (currency or None) and currency.upper(),
             notes, _now()),
        )
        _DB.commit()
        return int(cur.lastrowid)


def update_leg(leg_id: int, **fields) -> bool:
    allowed = {"kind", "carrier", "reference", "from_loc", "to_loc",
               "start_ts", "end_ts", "cost", "currency", "notes"}
    sets: list[str] = []
    vals: list = []
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k == "kind":
            v = _norm_kind(v)
        if k in ("start_ts", "end_ts"):
            v = _parse_ts(v)
        if k == "cost" and v is not None and v != "":
            v = float(v)
        if k == "currency" and v:
            v = v.upper()
        sets.append(f"{k}=?")
        vals.append(v)
    if not sets:
        return False
    vals.append(leg_id)
    with _LOCK:
        res = _DB.execute(
            f"UPDATE trip_legs SET {', '.join(sets)} WHERE id=?", vals
        )
        _DB.commit()
    return bool(res.rowcount)


def delete_leg(leg_id: int) -> bool:
    with _LOCK:
        res = _DB.execute("DELETE FROM trip_legs WHERE id=?", (leg_id,))
        _DB.commit()
    return bool(res.rowcount)


def list_legs(trip_id: int) -> list[dict]:
    with _LOCK:
        rows = _DB.execute(
            "SELECT * FROM trip_legs WHERE trip_id=? "
            "ORDER BY COALESCE(start_ts, 9e18), id",
            (trip_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def next_leg(trip_id: int | None = None,
             now: datetime | None = None) -> dict | None:
    """Return the next upcoming leg across a given trip (or across all
    trips when trip_id is None). Legs without a start_ts are skipped."""
    cutoff = (now or datetime.now()).timestamp()
    with _LOCK:
        if trip_id is None:
            row = _DB.execute(
                "SELECT * FROM trip_legs WHERE start_ts IS NOT NULL "
                "AND start_ts>=? ORDER BY start_ts LIMIT 1",
                (cutoff,),
            ).fetchone()
        else:
            row = _DB.execute(
                "SELECT * FROM trip_legs WHERE trip_id=? AND "
                "start_ts IS NOT NULL AND start_ts>=? "
                "ORDER BY start_ts LIMIT 1",
                (trip_id, cutoff),
            ).fetchone()
    return dict(row) if row else None


def trip_total_cost(trip_id: int) -> dict[str, float]:
    """Sum leg costs grouped by currency."""
    totals: dict[str, float] = {}
    for leg in list_legs(trip_id):
        if leg.get("cost") is None:
            continue
        ccy = leg.get("currency") or "USD"
        totals[ccy] = round(totals.get(ccy, 0.0) + float(leg["cost"]), 2)
    return totals
