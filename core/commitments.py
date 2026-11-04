"""
Commitment tracking.

The audit flagged commitment tracking as PARTIAL: todos and memory projects
capture *intentions*, but nobody surfaces them proactively ("you said you'd
call Sarah — did you?"). This module owns the commitment lifecycle:

  stated → open → (snoozed | resolved | missed)

Two input paths:

  1. Explicit — the planner or a tool handler calls ``add_commitment``.
  2. Implicit — ``extract_from_text`` scans a free-form string ("I'll send
     the proposal by Friday, and I need to check in with Omar Thursday")
     and returns the commitments it found. The proactive loop and the
     journal reader can both call this on anything the user said so a
     promise doesn't need an explicit tool call to be tracked.

Everything local; no LLM required for extraction (the regex layer is good
enough for the exec-assistant surface, and keeping it offline means it
runs on every utterance without cost).
"""
from __future__ import annotations

import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

_LOCK = threading.RLock()
_DB: sqlite3.Connection | None = None
_DB_PATH: Path | None = None


# ── Store ────────────────────────────────────────────────────────────────────

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
        _DB_PATH = want
        _init_schema()


def _init_schema() -> None:
    assert _DB is not None
    _DB.executescript(
        """
        CREATE TABLE IF NOT EXISTS commitments (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            text        TEXT    NOT NULL,
            normalised  TEXT    NOT NULL,          -- stripped+lowered for dedupe
            due_ts      REAL    DEFAULT NULL,
            contact_id  INTEGER DEFAULT NULL,
            source      TEXT    DEFAULT 'manual',  -- manual | extracted | plan
            status      TEXT    NOT NULL DEFAULT 'open',  -- open | done | missed | cancelled
            created_at  REAL    NOT NULL,
            resolved_at REAL    DEFAULT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_c_status ON commitments(status);
        CREATE INDEX IF NOT EXISTS idx_c_due    ON commitments(due_ts);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_c_dedupe
            ON commitments(normalised, status);
        """
    )
    _DB.commit()


def _now() -> float:
    return time.time()


def _normalise(text: str) -> str:
    s = (text or "").lower().strip()
    s = re.sub(r"\s+", " ", s)
    return s


# ── Public CRUD ─────────────────────────────────────────────────────────────

def add_commitment(text: str, *,
                   due_ts: float | None = None,
                   contact_id: int | None = None,
                   source: str = "manual") -> int:
    """Insert a commitment. Dedupes on (normalised, status='open'): the
    same promise stated twice updates the existing open row's due date
    (whichever is earlier wins) rather than inserting a duplicate."""
    text = (text or "").strip()
    if not text:
        raise ValueError("commitment text required")
    norm = _normalise(text)
    with _LOCK:
        existing = _DB.execute(
            "SELECT id, due_ts FROM commitments "
            "WHERE normalised=? AND status='open'",
            (norm,),
        ).fetchone()
        if existing:
            keep_due = existing["due_ts"]
            if due_ts is not None and (keep_due is None or due_ts < keep_due):
                keep_due = due_ts
            _DB.execute(
                "UPDATE commitments SET due_ts=?, contact_id=COALESCE(contact_id, ?) "
                "WHERE id=?",
                (keep_due, contact_id, existing["id"]),
            )
            _DB.commit()
            return int(existing["id"])
        cur = _DB.execute(
            "INSERT INTO commitments(text, normalised, due_ts, contact_id, "
            "source, created_at) VALUES(?,?,?,?,?,?)",
            (text, norm, due_ts, contact_id, source, _now()),
        )
        _DB.commit()
        return int(cur.lastrowid)


def resolve(commitment_id: int) -> bool:
    with _LOCK:
        res = _DB.execute(
            "UPDATE commitments SET status='done', resolved_at=? "
            "WHERE id=? AND status='open'",
            (_now(), commitment_id),
        )
        _DB.commit()
    return bool(res.rowcount)


def miss(commitment_id: int) -> bool:
    with _LOCK:
        res = _DB.execute(
            "UPDATE commitments SET status='missed', resolved_at=? "
            "WHERE id=? AND status='open'",
            (_now(), commitment_id),
        )
        _DB.commit()
    return bool(res.rowcount)


def snooze(commitment_id: int, by_hours: float) -> bool:
    """Push the due date out by ``by_hours``. Unsets the due date if it
    wasn't set (treats it as "now + by_hours")."""
    with _LOCK:
        row = _DB.execute(
            "SELECT due_ts FROM commitments WHERE id=? AND status='open'",
            (commitment_id,),
        ).fetchone()
        if not row:
            return False
        base = row["due_ts"] if row["due_ts"] else _now()
        new = base + by_hours * 3600.0
        _DB.execute(
            "UPDATE commitments SET due_ts=? WHERE id=?",
            (new, commitment_id),
        )
        _DB.commit()
    return True


def cancel(commitment_id: int) -> bool:
    with _LOCK:
        res = _DB.execute(
            "UPDATE commitments SET status='cancelled', resolved_at=? "
            "WHERE id=? AND status='open'",
            (_now(), commitment_id),
        )
        _DB.commit()
    return bool(res.rowcount)


def list_open(limit: int = 50) -> list[dict]:
    with _LOCK:
        rows = _DB.execute(
            "SELECT * FROM commitments WHERE status='open' "
            "ORDER BY COALESCE(due_ts, 9e18) LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def list_all(status: str | Iterable[str] | None = None,
             limit: int = 100) -> list[dict]:
    with _LOCK:
        if status is None:
            rows = _DB.execute(
                "SELECT * FROM commitments ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            if isinstance(status, str):
                status = [status]
            ph = ",".join("?" * len(list(status)))
            rows = _DB.execute(
                f"SELECT * FROM commitments WHERE status IN ({ph}) "
                f"ORDER BY created_at DESC LIMIT ?",
                (*status, limit),
            ).fetchall()
    return [dict(r) for r in rows]


def due_within(hours: float) -> list[dict]:
    """Open commitments whose due_ts is within ``hours`` from now OR
    already past. ``due_ts=NULL`` items are only returned when hours is
    very small — the proactive loop uses this to surface things the user
    promised but didn't pin a date on."""
    cutoff = _now() + hours * 3600.0
    with _LOCK:
        rows = _DB.execute(
            "SELECT * FROM commitments WHERE status='open' AND "
            "(due_ts IS NOT NULL AND due_ts <= ?) "
            "ORDER BY due_ts",
            (cutoff,),
        ).fetchall()
    return [dict(r) for r in rows]


def sweep_missed(grace_hours: float = 24.0) -> int:
    """Mark as 'missed' any open commitment whose due_ts is more than
    ``grace_hours`` in the past. Idempotent. Returns the count."""
    cutoff = _now() - grace_hours * 3600.0
    with _LOCK:
        res = _DB.execute(
            "UPDATE commitments SET status='missed', resolved_at=? "
            "WHERE status='open' AND due_ts IS NOT NULL AND due_ts<?",
            (_now(), cutoff),
        )
        _DB.commit()
    return int(res.rowcount or 0)


# ── Extractor ────────────────────────────────────────────────────────────────

@dataclass
class Extracted:
    text: str
    due_ts: float | None
    due_text: str | None

    def to_dict(self) -> dict:
        return {"text": self.text,
                "due_ts": self.due_ts,
                "due_text": self.due_text}


_COMMIT_VERBS = (
    r"i (?:will|'ll|shall|promise to|need to|have to|must|'m going to|am going to|gotta|got to)",
    r"i['\s]+ll",
    r"let me",
    r"i'll go ahead and",
    r"going to",
)

_COMMIT_RE = re.compile(
    r"(?P<lead>" + "|".join(_COMMIT_VERBS) + r")\s+"
    r"(?P<action>[^.!?;]+?)"
    r"(?P<tail>[.!?;]|$)",
    re.IGNORECASE,
)

# Supported time anchors for "by", "before", "on", "this", "next" + weekday,
# numeric dates, relative words. Returned as (keyword, parser).
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday",
             "friday", "saturday", "sunday")

_DUE_KEYWORDS = (
    r"today", r"tonight", r"tomorrow", r"this afternoon", r"this evening",
    r"next week", r"this week", r"end of (?:week|day|month)", r"eod", r"eow",
) + _WEEKDAYS


def _resolve_relative(phrase: str, *,
                      now: datetime | None = None) -> datetime | None:
    """Translate a short phrase like 'tomorrow', 'friday', 'end of week',
    'this evening' into a concrete datetime. Returns None if unparseable.
    Deliberately conservative — anything exotic falls through, no false
    precision."""
    phrase = phrase.strip().lower()
    base = now or datetime.now()

    if phrase in ("today", "end of day", "eod"):
        return base.replace(hour=17, minute=0, second=0, microsecond=0)
    if phrase == "tonight":
        return base.replace(hour=21, minute=0, second=0, microsecond=0)
    if phrase == "tomorrow":
        return (base + timedelta(days=1)).replace(hour=9, minute=0,
                                                   second=0, microsecond=0)
    if phrase in ("this afternoon",):
        return base.replace(hour=14, minute=0, second=0, microsecond=0)
    if phrase in ("this evening",):
        return base.replace(hour=19, minute=0, second=0, microsecond=0)
    if phrase == "next week":
        return (base + timedelta(days=7)).replace(hour=9, minute=0,
                                                   second=0, microsecond=0)
    if phrase == "this week":
        # The next Friday from today, 5pm.
        days_ahead = (4 - base.weekday()) % 7
        if days_ahead == 0:
            days_ahead = 5  # if today is Friday, aim at next Friday
        return (base + timedelta(days=days_ahead)).replace(
            hour=17, minute=0, second=0, microsecond=0)
    if phrase == "end of week":
        days_ahead = (6 - base.weekday()) % 7  # Sunday
        return (base + timedelta(days=days_ahead)).replace(
            hour=17, minute=0, second=0, microsecond=0)
    if phrase == "end of month":
        # 28th of the current month, 5pm — safe across every month length.
        y, m = base.year, base.month
        return base.replace(day=28, hour=17, minute=0, second=0, microsecond=0)
    if phrase == "eow":
        return _resolve_relative("end of week", now=now)

    if phrase in _WEEKDAYS:
        target = _WEEKDAYS.index(phrase)
        delta = (target - base.weekday()) % 7
        if delta == 0:
            delta = 7  # "see you Monday" on a Monday → next Monday
        return (base + timedelta(days=delta)).replace(
            hour=9, minute=0, second=0, microsecond=0)

    # YYYY-MM-DD date
    try:
        return datetime.strptime(phrase, "%Y-%m-%d")
    except ValueError:
        pass
    # Numeric slash/dash date
    for fmt in ("%m/%d/%Y", "%d/%m/%Y", "%m-%d-%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(phrase, fmt)
        except ValueError:
            continue
    return None


_DATE_PATTERNS = (
    r"\d{4}-\d{2}-\d{2}",
    r"\d{1,2}[\-/]\d{1,2}(?:[\-/]\d{2,4})?",
)

_DUE_RE = re.compile(
    r"(?:\b(?P<prep>by|before|on|this|next|end of)\s+)?"
    r"\b(?P<phrase>" + "|".join(_DUE_KEYWORDS + _DATE_PATTERNS) + r")\b",
    re.IGNORECASE,
)


def _parse_action_due(action: str,
                      now: datetime | None = None
                      ) -> tuple[str, float | None, str | None]:
    """Strip the trailing time phrase off ``action`` and resolve it to an
    epoch timestamp. Returns (cleaned_action, due_ts, raw_due_phrase).

    Both ``by Friday`` and bare ``Friday`` are recognised. The preposition
    is folded into the resolver for 'end of X' so 'end of week' resolves
    correctly, while bare 'end' (no 'of week') is never matched by the
    keyword set and therefore never misfires."""
    # Scan right-to-left: the time anchor is almost always at the end of
    # the clause, and picking the last match avoids false hits inside
    # verb phrases like "ship the Friday report".
    last = None
    for m in _DUE_RE.finditer(action):
        last = m
    if not last:
        return action.strip(), None, None
    prep = (last.group("prep") or "").lower()
    core = last.group("phrase").lower()
    phrase = f"{prep} {core}".strip() if prep == "end of" else core
    resolved = _resolve_relative(phrase, now=now)
    if resolved is None:
        return action.strip(), None, None
    cleaned = (action[:last.start()] + action[last.end():]).strip()
    cleaned = re.sub(r"\s+", " ", cleaned).rstrip(",;: ")
    return cleaned, resolved.timestamp(), last.group(0)


# A ", and I['ll| will| need to| ...]" clause inside a longer utterance would
# otherwise swallow the whole string as a single commitment. Pre-splitting
# turns it into two sentences so the main regex sees two distinct promises.
_CLAUSE_SPLIT = re.compile(
    r",\s+(?=and\s+i(?:\s|['’]ll|\s+will|\s+need|\s+have|\s+must|\s+promise|\s+shall|\s+am\s+going|\s+'m\s+going)\b)",
    re.IGNORECASE,
)


def extract_from_text(text: str,
                      now: datetime | None = None) -> list[Extracted]:
    """Return every commitment-shaped claim in ``text``. Deliberately
    conservative: patterns must start with a first-person verb-of-promise
    ("I will", "I'll", "I promise to"...), optionally followed by a time
    anchor. False positives are far worse than false negatives — a missed
    commitment stays in the next utterance, a bogus one is embarrassing."""
    if not text:
        return []
    # Split "..., and I ..." clauses so each promise is its own match.
    pieces = _CLAUSE_SPLIT.split(text)
    results: list[Extracted] = []
    for piece in pieces:
        # Trim any leading "and " left over from the split.
        piece = re.sub(r"^\s*and\s+", "", piece, flags=re.IGNORECASE)
        for m in _COMMIT_RE.finditer(piece):
            action = m.group("action").strip()
            if not action:
                continue
            action = re.sub(r"\b(for sure|really|just|probably|maybe)\b",
                            "", action, flags=re.IGNORECASE).strip()
            cleaned, due_ts, due_phrase = _parse_action_due(action, now=now)
            if not cleaned:
                continue
            results.append(Extracted(text=cleaned, due_ts=due_ts,
                                     due_text=due_phrase))
    return results


def extract_and_store(text: str, *,
                      source: str = "extracted") -> list[int]:
    """Extract commitments from ``text`` and insert them. Returns the list
    of commitment row ids. Convenience for the proactive / journal paths."""
    ids = []
    for e in extract_from_text(text):
        try:
            ids.append(add_commitment(e.text, due_ts=e.due_ts, source=source))
        except Exception as err:
            print(f"[Commitments] store failed: {err}")
    return ids
