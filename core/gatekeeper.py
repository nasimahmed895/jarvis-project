"""
Executive gatekeeper.

Classifies an inbound item (email, meeting request, call, generic message)
into a priority tier and an action recommendation so the exec-assistant
surface can decide what to escalate, what to flag, and what to ignore.

Design decisions
----------------
* Rules first, model second. The hot path (every inbound email) runs purely
  offline: CRM-tier lookup + keyword classifier. The LLM is only consulted
  when a caller explicitly asks for refinement on an ambiguous item — a
  mail shower at 2 AM should never block waiting on an API round trip.
* No side effects. ``classify`` returns a verdict; it never acts on it.
  That is by design — the voice loop makes the decision, not the gate.
* ``priority`` is a four-tier scale: P0 = act now (CEO-level, incident,
  wire-transfer deadline), P1 = today, P2 = this week, P3 = backlog.
* ``recommendation`` names the next move, not the feeling. 'escalate' means
  interrupt the user; 'flag' means surface on the next check-in;
  'normal' means leave it in the inbox; 'auto_decline' means it looks like
  marketing / unsubscribe / out-of-scope.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

try:
    from core import crm as _crm
except Exception:  # pragma: no cover — CRM is optional at classify time
    _crm = None  # type: ignore[assignment]


# Priority tiers, ordered highest urgency first.
PRIORITIES = ("P0", "P1", "P2", "P3")

# Base tier → default priority mapping. CRM tags are the strongest signal
# the gatekeeper has: a message from someone the user has explicitly
# tagged ``vip`` or ``investor`` deserves to interrupt by default.
_TIER_BASE_PRIORITY = {
    "vip":       "P0",
    "investor":  "P0",
    "client":    "P1",
    "colleague": "P2",
    "family":    "P1",   # family urgency beats work by default — the user
                         # can lower this by re-tagging if they disagree
    "vendor":    "P3",
    "regular":   "P3",
}

# Urgency keywords in subject/body. Each list pushes the priority UP by one
# rung (capped at P0). Localisation: include the most common English
# synonyms; the LLM pass handles the long tail.
_URGENCY_RE = re.compile(
    r"\b(urgent|asap|immediately|critical|emergency|deadline|"
    r"time[- ]?sensitive|now|today|tonight|eod|cob|"
    r"action required|please respond|needs your (attention|approval|signature))\b",
    re.IGNORECASE,
)

# Phrases that lower urgency or hint at auto-decline. We also tag messages
# with obvious newsletter / promotional markers so the recommendation
# flips from 'normal' to 'auto_decline' — the voice loop can still choose
# to show them, this is advice.
_PROMO_RE = re.compile(
    r"\b(unsubscribe|newsletter|promotion|promo|discount|%\s*off|"
    r"flash sale|black friday|cyber monday|limited time|gift card|"
    r"no[- ]reply|do not reply|noreply@)\b",
    re.IGNORECASE,
)

# Explicit high-stakes markers that force P0 regardless of sender tier.
# These are chosen to catch infrastructure-level events (security breach,
# wire fraud warning, legal notice) even when the sender is unknown.
_RED_FLAG_RE = re.compile(
    r"\b(security (?:alert|breach|incident)|account (?:compromise|breach)|"
    r"wire (?:transfer|fraud)|suspicious activity|legal notice|"
    r"subpoena|court order|data breach)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Verdict:
    priority: str
    source: str  # "vip" | "known" | "unknown"
    recommendation: str
    reasons: tuple[str, ...] = field(default_factory=tuple)
    tier: str = "regular"
    contact_id: int | None = None

    def to_dict(self) -> dict:
        return {
            "priority":       self.priority,
            "source":         self.source,
            "recommendation": self.recommendation,
            "reasons":        list(self.reasons),
            "tier":           self.tier,
            "contact_id":     self.contact_id,
        }


def _bump_priority(current: str, steps: int = 1) -> str:
    """Move ``current`` up by ``steps`` rungs (toward P0). Negative steps
    lower urgency. Clamped to the PRIORITIES range."""
    try:
        idx = PRIORITIES.index(current)
    except ValueError:
        idx = len(PRIORITIES) - 1
    new = max(0, min(len(PRIORITIES) - 1, idx - steps))
    return PRIORITIES[new]


def _lower_priority(current: str, steps: int = 1) -> str:
    return _bump_priority(current, -steps)


def _lookup_contact(sender_email: str | None,
                    sender_name: str | None) -> dict | None:
    """Try an exact-email match first, then a name substring. Returns None
    if CRM isn't available."""
    if _crm is None:
        return None
    try:
        if sender_email:
            c = _crm.find_by_email(sender_email)
            if c:
                return c
        if sender_name:
            rows = _crm.search_contacts(sender_name, limit=1)
            if rows:
                return rows[0]
    except Exception:
        return None
    return None


def classify(kind: str,
             *,
             subject: str | None = None,
             body: str | None = None,
             sender_email: str | None = None,
             sender_name: str | None = None) -> Verdict:
    """Classify a single inbound item. Pure function: no network, no DB
    writes, safe to call on every inbox item."""
    kind = (kind or "unknown").strip().lower() or "unknown"
    text = " ".join(filter(None, [subject, body])).strip()
    reasons: list[str] = []

    contact = _lookup_contact(sender_email, sender_name)
    tier = (contact or {}).get("tier", "regular")
    base = _TIER_BASE_PRIORITY.get(tier, "P3")
    reasons.append(f"tier={tier}")

    if contact:
        source = "vip" if tier in ("vip", "investor") else "known"
        reasons.append(f"known-contact#{contact['id']}:{contact.get('name','?')}")
    else:
        source = "unknown"
        reasons.append("unknown-sender")

    priority = base

    if _RED_FLAG_RE.search(text):
        priority = "P0"
        reasons.append("red-flag-keyword")

    urgency_hits = len(_URGENCY_RE.findall(text))
    if urgency_hits:
        # Capped: a single urgency word moves you up one rung; piling on
        # more does NOT push past P0 (and the cap stops a spam message
        # stuffed with 'URGENT URGENT URGENT' from forcing an interrupt).
        priority = _bump_priority(priority, 1)
        reasons.append(f"urgency-keyword×{urgency_hits}")

    promo_hits = len(_PROMO_RE.findall(text))
    if promo_hits and source == "unknown":
        # Only lower priority when we don't trust the sender. A real client
        # mailing a holiday promo deserves to still be P1.
        priority = _lower_priority(priority, 1)
        reasons.append(f"promo-keyword×{promo_hits}")

    # Meeting requests from unknown senders deserve 'flag' rather than
    # 'auto_decline': you still want the exec to see "this stranger wants
    # 30 minutes" even if the priority is P3. ``min`` by index pulls P3 up
    # to P2 while leaving P0/P1 alone (lower index = higher urgency).
    if kind in ("meeting", "meeting_request") and source == "unknown":
        priority = min(priority, "P2", key=PRIORITIES.index)

    # Recommendation derived from priority + source.
    if promo_hits and source == "unknown":
        recommendation = "auto_decline"
    elif priority == "P0":
        recommendation = "escalate"
    elif priority == "P1":
        recommendation = "flag"
    elif priority == "P2":
        recommendation = "flag" if source != "unknown" else "normal"
    else:
        recommendation = "normal"

    return Verdict(
        priority=priority,
        source=source,
        recommendation=recommendation,
        reasons=tuple(reasons),
        tier=tier,
        contact_id=(contact or {}).get("id"),
    )


# ── Batch helpers ────────────────────────────────────────────────────────────

def priority_queue(items: Iterable[dict], *, limit: int = 20) -> list[dict]:
    """Classify a list of items (each a dict with subject/body/sender_*/kind)
    and return them sorted by urgency then unread newest-first.

    The input dicts are returned augmented with a ``verdict`` key so callers
    can thread this through display logic without re-classifying.
    """
    scored: list[tuple[int, int, dict]] = []
    for i, it in enumerate(items or ()):
        v = classify(
            kind=it.get("kind") or "email",
            subject=it.get("subject"),
            body=it.get("body"),
            sender_email=it.get("sender_email") or it.get("from"),
            sender_name=it.get("sender_name") or it.get("name"),
        )
        try:
            rank = PRIORITIES.index(v.priority)
        except ValueError:
            rank = len(PRIORITIES)
        enriched = dict(it)
        enriched["verdict"] = v.to_dict()
        # Preserve input order as the secondary sort — most inbox APIs
        # deliver newest-first, and callers expect that to survive.
        scored.append((rank, i, enriched))
    scored.sort(key=lambda t: (t[0], t[1]))
    return [row for _, _, row in scored[:limit]]


# ── Availability + conflict helpers ─────────────────────────────────────────

def detect_conflict(events: Iterable[dict], start_iso: str,
                    end_iso: str | None = None) -> list[dict]:
    """Return events from ``events`` whose window overlaps [start, end].

    Each event must have ``start`` and optionally ``end`` as ISO strings
    (``YYYY-MM-DD HH:MM`` is the shape ``_mac_apps.calendar_list_events``
    emits). Mid-airport safe: pure string/date math, no tz library required
    beyond the standard ``datetime`` parser.
    """
    from datetime import datetime, timedelta

    def parse(s: str | None) -> datetime | None:
        if not s:
            return None
        s = s.strip()
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M",
                    "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
                    "%Y-%m-%d"):
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                continue
        return None

    req_start = parse(start_iso)
    req_end = parse(end_iso) if end_iso else (
        req_start + timedelta(hours=1) if req_start else None
    )
    if req_start is None or req_end is None:
        return []

    conflicts: list[dict] = []
    for ev in events or ():
        ev_start = parse(ev.get("start"))
        ev_end = parse(ev.get("end")) or (
            ev_start + timedelta(hours=1) if ev_start else None
        )
        if ev_start is None or ev_end is None:
            continue
        if ev_start < req_end and ev_end > req_start:
            conflicts.append(ev)
    return conflicts
