"""
core/predictions.py — prediction history, outcome recording, and calibration.

The existing `core/predictor.py` produces *suggestions* (recurring-slot
patterns distilled from the journal). That module is intentionally thin and
has no notion of "was the suggestion right?". This module is the ledger
that closes the loop:

  1. `record(...)` — stash a prediction the moment it is generated, with
      full provenance (category, subject, horizon, point estimate /
      probability, confidence, assumptions).
  2. `resolve(id, actual=..., observed_at=...)` — the outcome becomes
      known (or the horizon passes); the row is updated and an error
      metric is computed.
  3. `metrics()` — aggregate MAE / MAPE for numerical predictions and
      Brier score + calibration buckets for probability predictions,
      scoped to a category / horizon window.

Deliberate choices
------------------
* **SQLite in memory/predictions.sqlite**, matching the pattern used by
  `core/tasks.py` and `core/notify.py`. One table, one connection,
  one lock. No ORM.
* **No ML parameter updates.** This is a ledger and a scoreboard, not a
  trainer. Calibration metrics feed the dashboard; the model learns by the
  user (or operator) looking at those numbers and deciding what to change.
* Prediction categories are strings (not an enum) so new modules can
  introduce their own without a schema migration.
* Numerical predictions carry a `point` (float). Probability predictions
  carry `point` ∈ [0, 1] and are flagged via `kind="probability"`; the
  resolver then expects `actual` ∈ {0, 1}.
"""
from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional


_DB: sqlite3.Connection | None = None
_DB_PATH: Path | None = None
_LOCK = threading.RLock()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS predictions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    category        TEXT NOT NULL,          -- behavioral, time, financial, ...
    kind            TEXT NOT NULL,          -- "numeric" | "probability" | "label"
    subject         TEXT NOT NULL,          -- "project X completion", "SUB-123 renew"
    point           REAL,                   -- scalar forecast (date-as-epoch, amount, prob)
    lower           REAL,                   -- optional CI lower
    upper           REAL,                   -- optional CI upper
    confidence      REAL,                   -- model-reported, in [0,1]
    horizon_s       INTEGER,                -- seconds from created_at the prediction refers to
    model           TEXT,                   -- e.g. "predictor.suggest v1", "risk.timebox"
    evidence_json   TEXT,                   -- small JSON blob: sources + counts
    assumptions     TEXT,                   -- free text, operator-readable
    created_at      REAL NOT NULL,
    resolved_at     REAL,
    actual          REAL,                   -- numeric or 0/1
    actual_label    TEXT,                   -- optional string outcome for kind=label
    error           REAL,                   -- abs(actual - point) for numeric
                                            -- or (actual - point) for probability
    status          TEXT NOT NULL           -- "open" | "resolved" | "cancelled"
);
CREATE INDEX IF NOT EXISTS idx_predictions_status   ON predictions(status);
CREATE INDEX IF NOT EXISTS idx_predictions_category ON predictions(category);
CREATE INDEX IF NOT EXISTS idx_predictions_created  ON predictions(created_at DESC);
"""


def connect(db_path: Path | str) -> None:
    """Open or reuse the predictions database."""
    global _DB, _DB_PATH
    path = Path(db_path)
    with _LOCK:
        if _DB is not None and _DB_PATH == path:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        _DB = sqlite3.connect(str(path), check_same_thread=False,
                              isolation_level=None)
        _DB.row_factory = sqlite3.Row
        _DB.executescript(_SCHEMA)
        _DB_PATH = path


def _db() -> sqlite3.Connection:
    if _DB is None:
        # Default location matches the rest of the project.
        default = Path(__file__).resolve().parent.parent / "memory" / "predictions.sqlite"
        connect(default)
    assert _DB is not None
    return _DB


def _now() -> float:
    return time.time()


# ── Recording ───────────────────────────────────────────────────────────

@dataclass
class Prediction:
    id: int
    category: str
    kind: str
    subject: str
    point: Optional[float]
    lower: Optional[float]
    upper: Optional[float]
    confidence: Optional[float]
    horizon_s: Optional[int]
    model: Optional[str]
    evidence: dict
    assumptions: Optional[str]
    created_at: float
    resolved_at: Optional[float]
    actual: Optional[float]
    actual_label: Optional[str]
    error: Optional[float]
    status: str

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> "Prediction":
        return cls(
            id=int(r["id"]),
            category=r["category"],
            kind=r["kind"],
            subject=r["subject"],
            point=r["point"],
            lower=r["lower"],
            upper=r["upper"],
            confidence=r["confidence"],
            horizon_s=r["horizon_s"],
            model=r["model"],
            evidence=json.loads(r["evidence_json"] or "{}"),
            assumptions=r["assumptions"],
            created_at=r["created_at"],
            resolved_at=r["resolved_at"],
            actual=r["actual"],
            actual_label=r["actual_label"],
            error=r["error"],
            status=r["status"],
        )

    def to_dict(self) -> dict:
        return {
            "id":           self.id,
            "category":     self.category,
            "kind":         self.kind,
            "subject":      self.subject,
            "point":        self.point,
            "lower":        self.lower,
            "upper":        self.upper,
            "confidence":   self.confidence,
            "horizon_s":    self.horizon_s,
            "model":        self.model,
            "evidence":     self.evidence,
            "assumptions":  self.assumptions,
            "created_at":   self.created_at,
            "resolved_at":  self.resolved_at,
            "actual":       self.actual,
            "actual_label": self.actual_label,
            "error":        self.error,
            "status":       self.status,
        }


def record(category: str, subject: str, *,
           kind: str = "numeric",
           point: Optional[float] = None,
           lower: Optional[float] = None,
           upper: Optional[float] = None,
           confidence: Optional[float] = None,
           horizon_s: Optional[int] = None,
           model: Optional[str] = None,
           evidence: Optional[dict] = None,
           assumptions: Optional[str] = None) -> int:
    """Insert a prediction row and return its id."""
    if kind not in ("numeric", "probability", "label"):
        raise ValueError(f"unknown kind: {kind}")
    if kind == "probability" and point is not None and not 0.0 <= point <= 1.0:
        raise ValueError("probability predictions require point in [0,1]")
    if confidence is not None and not 0.0 <= confidence <= 1.0:
        raise ValueError("confidence must be in [0,1]")
    with _LOCK:
        cur = _db().execute(
            "INSERT INTO predictions("
            "category, kind, subject, point, lower, upper, confidence, "
            "horizon_s, model, evidence_json, assumptions, created_at, status"
            ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')",
            (category, kind, subject, point, lower, upper, confidence,
             horizon_s, model, json.dumps(evidence or {}),
             assumptions, _now()),
        )
        return int(cur.lastrowid)


def cancel(prediction_id: int) -> None:
    """Mark a prediction as cancelled — the subject no longer applies
    (e.g. the project was shelved before the horizon)."""
    with _LOCK:
        _db().execute(
            "UPDATE predictions SET status='cancelled' WHERE id=?",
            (prediction_id,))


def resolve(prediction_id: int,
            actual: Optional[float] = None,
            actual_label: Optional[str] = None,
            observed_at: Optional[float] = None) -> Optional[float]:
    """Record the actual outcome. Returns the computed error (or None for
    label predictions)."""
    with _LOCK:
        row = _db().execute(
            "SELECT * FROM predictions WHERE id=?", (prediction_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"no prediction {prediction_id}")
        if row["status"] != "open":
            raise ValueError(f"prediction {prediction_id} is "
                             f"{row['status']}, not open")
        kind = row["kind"]
        point = row["point"]
        err: Optional[float] = None
        if kind == "numeric":
            if actual is None:
                raise ValueError("numeric predictions require `actual`")
            err = abs(float(actual) - float(point)) if point is not None else None
        elif kind == "probability":
            if actual is None or actual not in (0, 1, 0.0, 1.0):
                raise ValueError("probability predictions require actual in {0,1}")
            err = float(actual) - float(point) if point is not None else None
        elif kind == "label":
            if not actual_label:
                raise ValueError("label predictions require `actual_label`")
            err = None
        _db().execute(
            "UPDATE predictions SET status='resolved', resolved_at=?, "
            "actual=?, actual_label=?, error=? WHERE id=?",
            (observed_at or _now(), actual, actual_label, err, prediction_id),
        )
        return err


# ── Retrieval ───────────────────────────────────────────────────────────

def get(prediction_id: int) -> Optional[Prediction]:
    with _LOCK:
        row = _db().execute(
            "SELECT * FROM predictions WHERE id=?", (prediction_id,),
        ).fetchone()
        return Prediction.from_row(row) if row else None


def list_predictions(*, category: Optional[str] = None,
                     status: Optional[str] = None,
                     limit: int = 100) -> list[Prediction]:
    q = "SELECT * FROM predictions"
    clauses: list[str] = []
    args: list[Any] = []
    if category:
        clauses.append("category=?")
        args.append(category)
    if status:
        clauses.append("status=?")
        args.append(status)
    if clauses:
        q += " WHERE " + " AND ".join(clauses)
    q += " ORDER BY created_at DESC LIMIT ?"
    args.append(limit)
    with _LOCK:
        rows = _db().execute(q, args).fetchall()
        return [Prediction.from_row(r) for r in rows]


# ── Calibration metrics ─────────────────────────────────────────────────

def metrics(*, category: Optional[str] = None,
            kind: Optional[str] = None) -> dict:
    """Compute MAE / MAPE for numeric predictions and Brier + calibration
    buckets for probability predictions. Scoped to `category` / `kind` when
    given. Values are None when there is insufficient data."""
    q = ("SELECT kind, point, actual, error FROM predictions "
         "WHERE status='resolved'")
    args: list[Any] = []
    if category:
        q += " AND category=?"
        args.append(category)
    if kind:
        q += " AND kind=?"
        args.append(kind)
    with _LOCK:
        rows = _db().execute(q, args).fetchall()

    numeric = [(float(r["point"]), float(r["actual"]), float(r["error"]))
               for r in rows
               if r["kind"] == "numeric"
               and r["point"] is not None and r["actual"] is not None
               and r["error"] is not None]
    probs = [(float(r["point"]), float(r["actual"]))
             for r in rows
             if r["kind"] == "probability"
             and r["point"] is not None and r["actual"] is not None]

    out: dict[str, Any] = {
        "numeric_n":       len(numeric),
        "probability_n":   len(probs),
    }

    if numeric:
        mae = sum(e for _, _, e in numeric) / len(numeric)
        out["mae"] = mae
        # MAPE: only meaningful when actual != 0; skip rows where actual is 0.
        mape_vals = [abs(p - a) / abs(a) for p, a, _ in numeric if a]
        if mape_vals:
            out["mape"] = sum(mape_vals) / len(mape_vals)
        # RMSE as a bonus — sensitive to large errors.
        out["rmse"] = math.sqrt(sum(e * e for _, _, e in numeric) / len(numeric))

    if probs:
        brier = sum((p - a) ** 2 for p, a in probs) / len(probs)
        out["brier"] = brier
        # Reliability diagram buckets of width 0.1. Report (bin, mean_pred,
        # mean_actual, n). Buckets with n == 0 are omitted.
        buckets: list[dict[str, float]] = []
        for lo in [i / 10 for i in range(10)]:
            hi = lo + 0.1
            in_bucket = [(p, a) for p, a in probs if lo <= p < hi or
                         (hi == 1.0 and p == 1.0)]
            if not in_bucket:
                continue
            mean_pred = sum(p for p, _ in in_bucket) / len(in_bucket)
            mean_actual = sum(a for _, a in in_bucket) / len(in_bucket)
            buckets.append({"lo": lo, "hi": hi,
                            "mean_pred": mean_pred,
                            "mean_actual": mean_actual,
                            "n": len(in_bucket)})
        out["calibration"] = buckets

    return out


# ── Housekeeping ────────────────────────────────────────────────────────

def expire_overdue(now: Optional[float] = None) -> int:
    """Mark open predictions whose horizon has elapsed by more than 7 days
    as cancelled when no outcome has been recorded. Returns the count so
    callers can log it."""
    now = now or _now()
    cutoff_offset = 7 * 24 * 3600
    with _LOCK:
        cur = _db().execute(
            "UPDATE predictions SET status='cancelled' "
            "WHERE status='open' AND horizon_s IS NOT NULL "
            "AND (created_at + horizon_s + ?) < ?",
            (cutoff_offset, now),
        )
        return cur.rowcount or 0


def delete(prediction_id: int) -> None:
    with _LOCK:
        _db().execute("DELETE FROM predictions WHERE id=?", (prediction_id,))
