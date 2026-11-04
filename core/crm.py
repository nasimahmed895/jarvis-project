"""
Lightweight CRM + stakeholder store.

Separate from the macOS Contacts.app bridge in ``actions/contacts_manager.py``
(which stays read-only, because syncing JARVIS's own tags into the system
address book would surprise anyone else reading it). This module owns:

  - Contacts JARVIS considers notable enough to tag or follow up on.
  - A tier field (``vip`` / ``client`` / ``colleague`` / ``family`` /
    ``vendor`` / ``regular``) that drives gatekeeping decisions.
  - Interaction log (emails sent, meetings held, calls, notes) keyed to a
    contact so JARVIS can answer "when did I last hear from Sarah?" without
    scraping the mail store every time.
  - Follow-up queue: contact-anchored reminders for "ping back by Friday".

Everything lives in ``memory/crm.sqlite``. Schema is deliberately small — a
real CRM is an entire product; this layer only has to hold enough state for
the exec-assistant features (gatekeeper, meeting prep, follow-ups) to work.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Iterable

_LOCK = threading.RLock()
_DB: sqlite3.Connection | None = None
_DB_PATH: Path | None = None


# Canonical tiers in priority order (highest first). Callers pass any string;
# anything not in this set falls back to 'regular'. A fixed list keeps the
# gatekeeper rules simple — no surprise tiers appear from freeform tagging.
TIERS = ("vip", "client", "investor", "colleague", "family", "vendor", "regular")

# Interaction kinds we track. Open-ended string on write (freeform notes are
# fine), these are just the ones the aggregator understands for summaries.
INTERACTION_KINDS = ("email_in", "email_out", "meeting", "call",
                     "message_in", "message_out", "note")


def connect(db_path: Path | str) -> None:
    """Open or reuse the CRM database."""
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
        CREATE TABLE IF NOT EXISTS contacts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT    NOT NULL,
            tier        TEXT    NOT NULL DEFAULT 'regular',
            email       TEXT    DEFAULT NULL,
            phone       TEXT    DEFAULT NULL,
            org         TEXT    DEFAULT NULL,
            role        TEXT    DEFAULT NULL,
            notes       TEXT    DEFAULT NULL,
            birthday    TEXT    DEFAULT NULL,
            created_at  REAL    NOT NULL,
            updated_at  REAL    NOT NULL,
            last_seen   REAL    DEFAULT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_contacts_name  ON contacts(name);
        CREATE INDEX IF NOT EXISTS idx_contacts_email ON contacts(email);
        CREATE INDEX IF NOT EXISTS idx_contacts_tier  ON contacts(tier);

        CREATE TABLE IF NOT EXISTS interactions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            contact_id  INTEGER NOT NULL,
            kind        TEXT    NOT NULL,
            summary     TEXT    DEFAULT NULL,
            subject     TEXT    DEFAULT NULL,
            ts          REAL    NOT NULL,
            FOREIGN KEY (contact_id) REFERENCES contacts(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_interactions_contact ON interactions(contact_id);
        CREATE INDEX IF NOT EXISTS idx_interactions_ts      ON interactions(ts);

        CREATE TABLE IF NOT EXISTS follow_ups (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            contact_id  INTEGER DEFAULT NULL,
            text        TEXT    NOT NULL,
            due_ts      REAL    DEFAULT NULL,
            status      TEXT    NOT NULL DEFAULT 'open',
            created_at  REAL    NOT NULL,
            resolved_at REAL    DEFAULT NULL,
            FOREIGN KEY (contact_id) REFERENCES contacts(id) ON DELETE SET NULL
        );
        CREATE INDEX IF NOT EXISTS idx_followups_status ON follow_ups(status);
        CREATE INDEX IF NOT EXISTS idx_followups_due    ON follow_ups(due_ts);
        """
    )
    _DB.commit()


def _now() -> float:
    return time.time()


def _norm_tier(tier: str | None) -> str:
    t = (tier or "").strip().lower()
    return t if t in TIERS else "regular"


# ── Contacts ─────────────────────────────────────────────────────────────────

def add_contact(name: str, *, email: str | None = None,
                phone: str | None = None, org: str | None = None,
                role: str | None = None, tier: str = "regular",
                birthday: str | None = None,
                notes: str | None = None) -> int:
    """Insert a contact and return its id. Duplicate names are allowed — the
    caller already picked a canonical name; collapsing duplicates is the
    user's call (via merge_contact, not enforced here)."""
    assert _DB is not None, "crm.connect() not called"
    name = (name or "").strip()
    if not name:
        raise ValueError("contact name required")
    now = _now()
    with _LOCK:
        cur = _DB.execute(
            "INSERT INTO contacts(name, tier, email, phone, org, role, "
            "notes, birthday, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (name, _norm_tier(tier), email, phone, org, role, notes,
             birthday, now, now),
        )
        _DB.commit()
        return int(cur.lastrowid)


def update_contact(contact_id: int, **fields) -> bool:
    """Patch columns on a contact. Unknown keys are silently ignored so a
    future column addition doesn't break existing callers."""
    allowed = {"name", "tier", "email", "phone", "org", "role", "notes",
               "birthday", "last_seen"}
    sets: list[str] = []
    vals: list = []
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k == "tier":
            v = _norm_tier(v)
        sets.append(f"{k}=?")
        vals.append(v)
    if not sets:
        return False
    sets.append("updated_at=?")
    vals.append(_now())
    vals.append(contact_id)
    with _LOCK:
        res = _DB.execute(
            f"UPDATE contacts SET {', '.join(sets)} WHERE id=?", vals
        )
        _DB.commit()
    return bool(res.rowcount)


def set_tier(contact_id: int, tier: str) -> bool:
    return update_contact(contact_id, tier=_norm_tier(tier))


def delete_contact(contact_id: int) -> bool:
    with _LOCK:
        res = _DB.execute("DELETE FROM contacts WHERE id=?", (contact_id,))
        _DB.commit()
    return bool(res.rowcount)


def get_contact(contact_id: int) -> dict | None:
    with _LOCK:
        row = _DB.execute(
            "SELECT * FROM contacts WHERE id=?", (contact_id,)
        ).fetchone()
    return dict(row) if row else None


def find_by_email(email: str) -> dict | None:
    """Case-insensitive lookup — the first match wins. Keeps it cheap for
    the gatekeeper's hot path (every inbound email asks this question)."""
    email = (email or "").strip().lower()
    if not email:
        return None
    with _LOCK:
        row = _DB.execute(
            "SELECT * FROM contacts WHERE LOWER(email)=? LIMIT 1", (email,)
        ).fetchone()
    return dict(row) if row else None


def list_contacts(tier: str | Iterable[str] | None = None,
                  limit: int = 100) -> list[dict]:
    with _LOCK:
        if tier is None:
            rows = _DB.execute(
                "SELECT * FROM contacts ORDER BY tier, name LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            tiers = [tier] if isinstance(tier, str) else list(tier)
            tiers = [_norm_tier(t) for t in tiers]
            ph = ",".join("?" * len(tiers))
            rows = _DB.execute(
                f"SELECT * FROM contacts WHERE tier IN ({ph}) "
                f"ORDER BY tier, name LIMIT ?",
                (*tiers, limit),
            ).fetchall()
    return [dict(r) for r in rows]


def search_contacts(query: str, limit: int = 20) -> list[dict]:
    """Substring search across name / email / org / role. Case-insensitive.
    Used by the model when the user says a partial name or "the person at X"."""
    q = f"%{(query or '').strip().lower()}%"
    if q == "%%":
        return list_contacts(limit=limit)
    with _LOCK:
        rows = _DB.execute(
            "SELECT * FROM contacts WHERE "
            "LOWER(name)   LIKE ? OR "
            "LOWER(email)  LIKE ? OR "
            "LOWER(org)    LIKE ? OR "
            "LOWER(role)   LIKE ? OR "
            "LOWER(notes)  LIKE ? "
            "ORDER BY "
            "  CASE tier WHEN 'vip' THEN 0 WHEN 'investor' THEN 1 "
            "            WHEN 'client' THEN 2 WHEN 'colleague' THEN 3 "
            "            WHEN 'family' THEN 4 ELSE 5 END, "
            "  name "
            "LIMIT ?",
            (q, q, q, q, q, limit),
        ).fetchall()
    return [dict(r) for r in rows]


# ── Interactions ─────────────────────────────────────────────────────────────

def log_interaction(contact_id: int, kind: str, *,
                    summary: str | None = None,
                    subject: str | None = None,
                    ts: float | None = None) -> int:
    """Record one touch-point with a contact. Freeform kind is allowed so
    callers can invent categories ('coffee', 'intro_call'); the summaries
    API treats anything outside INTERACTION_KINDS as 'other'. Also updates
    the contact's ``last_seen`` so the gatekeeper can age relationships."""
    ts = ts if ts is not None else _now()
    with _LOCK:
        cur = _DB.execute(
            "INSERT INTO interactions(contact_id, kind, summary, subject, ts) "
            "VALUES(?,?,?,?,?)",
            (contact_id, kind, summary, subject, ts),
        )
        _DB.execute(
            "UPDATE contacts SET last_seen=?, updated_at=? WHERE id=?",
            (ts, _now(), contact_id),
        )
        _DB.commit()
        return int(cur.lastrowid)


def recent_interactions(contact_id: int, limit: int = 20) -> list[dict]:
    with _LOCK:
        rows = _DB.execute(
            "SELECT * FROM interactions WHERE contact_id=? "
            "ORDER BY ts DESC LIMIT ?",
            (contact_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def days_since_contact(contact_id: int) -> float | None:
    c = get_contact(contact_id)
    if not c or not c.get("last_seen"):
        return None
    return max(0.0, (_now() - float(c["last_seen"])) / 86400.0)


# ── Follow-ups ───────────────────────────────────────────────────────────────

def add_follow_up(text: str, *, contact_id: int | None = None,
                  due_ts: float | None = None) -> int:
    with _LOCK:
        cur = _DB.execute(
            "INSERT INTO follow_ups(contact_id, text, due_ts, created_at) "
            "VALUES(?,?,?,?)",
            (contact_id, text, due_ts, _now()),
        )
        _DB.commit()
        return int(cur.lastrowid)


def resolve_follow_up(follow_up_id: int) -> bool:
    with _LOCK:
        res = _DB.execute(
            "UPDATE follow_ups SET status='done', resolved_at=? "
            "WHERE id=? AND status='open'",
            (_now(), follow_up_id),
        )
        _DB.commit()
    return bool(res.rowcount)


def due_follow_ups(within_hours: float | None = 24.0) -> list[dict]:
    """Open follow-ups whose due_ts is within `within_hours` from now (or any
    due_ts in the past). ``within_hours=None`` returns every open follow-up."""
    now = _now()
    with _LOCK:
        if within_hours is None:
            rows = _DB.execute(
                "SELECT f.*, c.name AS contact_name, c.tier AS contact_tier "
                "FROM follow_ups f LEFT JOIN contacts c ON f.contact_id=c.id "
                "WHERE f.status='open' "
                "ORDER BY COALESCE(f.due_ts, 9e18)"
            ).fetchall()
        else:
            cutoff = now + within_hours * 3600.0
            rows = _DB.execute(
                "SELECT f.*, c.name AS contact_name, c.tier AS contact_tier "
                "FROM follow_ups f LEFT JOIN contacts c ON f.contact_id=c.id "
                "WHERE f.status='open' AND "
                "(f.due_ts IS NULL OR f.due_ts <= ?) "
                "ORDER BY COALESCE(f.due_ts, 9e18)",
                (cutoff,),
            ).fetchall()
    return [dict(r) for r in rows]


def list_follow_ups(status: str = "open", limit: int = 50) -> list[dict]:
    with _LOCK:
        rows = _DB.execute(
            "SELECT f.*, c.name AS contact_name, c.tier AS contact_tier "
            "FROM follow_ups f LEFT JOIN contacts c ON f.contact_id=c.id "
            "WHERE f.status=? ORDER BY f.created_at DESC LIMIT ?",
            (status, limit),
        ).fetchall()
    return [dict(r) for r in rows]


# ── Convenience ──────────────────────────────────────────────────────────────

def tier_rank(tier: str) -> int:
    """Lower rank = more important. Used by the gatekeeper and inbox sorter.
    Unknown tiers fall through to 'regular'."""
    try:
        return TIERS.index(_norm_tier(tier))
    except ValueError:
        return len(TIERS)


def stats() -> dict:
    with _LOCK:
        totals = {
            r["tier"]: int(r["n"]) for r in _DB.execute(
                "SELECT tier, COUNT(*) AS n FROM contacts GROUP BY tier"
            ).fetchall()
        }
        open_fu = _DB.execute(
            "SELECT COUNT(*) FROM follow_ups WHERE status='open'"
        ).fetchone()[0]
        inter = _DB.execute(
            "SELECT COUNT(*) FROM interactions"
        ).fetchone()[0]
    return {"contacts_by_tier": totals, "open_follow_ups": int(open_fu),
            "interactions": int(inter)}
