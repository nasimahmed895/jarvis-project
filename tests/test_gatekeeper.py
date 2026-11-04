"""Tests for core/gatekeeper.py classifier + batch helpers."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import gatekeeper as gk
from core import crm


def _fresh_crm() -> None:
    crm.connect(Path(tempfile.mktemp(suffix="-gk.sqlite")))


def test_unknown_sender_default_p3():
    _fresh_crm()
    v = gk.classify("email", subject="hello", body="just saying hi",
                    sender_email="rando@nowhere.io")
    assert v.priority == "P3"
    assert v.source == "unknown"
    assert v.recommendation == "normal"


def test_vip_sender_p0_escalate():
    _fresh_crm()
    crm.add_contact("Jane", email="jane@acme.com", tier="vip")
    v = gk.classify("email", subject="quick note", body="let's catch up",
                    sender_email="jane@acme.com")
    assert v.priority == "P0"
    assert v.source == "vip"
    assert v.recommendation == "escalate"


def test_investor_also_vip_source():
    _fresh_crm()
    crm.add_contact("Lee", email="lee@fund.vc", tier="investor")
    v = gk.classify("email", subject="follow-up",
                    sender_email="lee@fund.vc")
    assert v.source == "vip"
    assert v.priority == "P0"


def test_urgency_bumps_priority():
    _fresh_crm()
    crm.add_contact("Rob", email="rob@co.com", tier="colleague")
    v = gk.classify("email", subject="URGENT: production down",
                    body="please fix ASAP",
                    sender_email="rob@co.com")
    # colleague base is P2, urgency bumps +1 → P1
    assert v.priority == "P1"


def test_red_flag_forces_p0_even_unknown():
    _fresh_crm()
    v = gk.classify("email",
                    subject="Security alert on your account",
                    body="we noticed suspicious activity",
                    sender_email="no-reply@fake.co")
    # No-reply is a promo keyword → would normally lower priority, but
    # red-flag overrides.
    assert v.priority == "P0"
    assert "red-flag" in " ".join(v.reasons)


def test_promo_unknown_auto_declines():
    _fresh_crm()
    v = gk.classify("email",
                    subject="30% off your next order — unsubscribe here",
                    body="limited time promo",
                    sender_email="noreply@shop.com")
    assert v.recommendation == "auto_decline"


def test_promo_known_still_flagged():
    _fresh_crm()
    crm.add_contact("Partner", email="p@partner.com", tier="client")
    v = gk.classify("email",
                    subject="Holiday promo from our team",
                    sender_email="p@partner.com")
    assert v.recommendation != "auto_decline"


def test_meeting_from_unknown_bumps_to_p2():
    _fresh_crm()
    v = gk.classify("meeting_request", subject="30 min chat?",
                    body="would love to intro myself",
                    sender_email="stranger@invite.io")
    assert v.priority in ("P0", "P1", "P2")
    assert v.source == "unknown"


def test_priority_queue_orders_correctly():
    _fresh_crm()
    crm.add_contact("CEO", email="ceo@biz.com", tier="vip")
    crm.add_contact("Teammate", email="tm@biz.com", tier="colleague")
    inbox = [
        {"subject": "lunch?", "sender_email": "tm@biz.com", "kind": "email"},
        {"subject": "pls look", "sender_email": "ceo@biz.com", "kind": "email"},
        {"subject": "spam", "sender_email": "rando@x.io",
         "body": "unsubscribe", "kind": "email"},
    ]
    ranked = gk.priority_queue(inbox)
    # VIP first, colleague next, promo-unknown last
    assert ranked[0]["sender_email"] == "ceo@biz.com"
    assert ranked[1]["sender_email"] == "tm@biz.com"
    assert ranked[-1]["sender_email"] == "rando@x.io"


def test_detect_conflict_basic():
    events = [
        {"start": "2026-11-05 10:00", "end": "2026-11-05 11:00",
         "title": "A"},
        {"start": "2026-11-05 14:00", "end": "2026-11-05 15:00",
         "title": "B"},
    ]
    assert len(gk.detect_conflict(events, "2026-11-05 10:30",
                                  "2026-11-05 11:30")) == 1
    assert len(gk.detect_conflict(events, "2026-11-05 12:00",
                                  "2026-11-05 13:00")) == 0
    # Edge: back-to-back touches but doesn't overlap
    assert len(gk.detect_conflict(events, "2026-11-05 11:00",
                                  "2026-11-05 12:00")) == 0


def test_detect_conflict_tolerates_partial_events():
    events = [
        {"start": "2026-11-05 09:00", "title": "no-end"},  # 1h default
    ]
    # proposed 09:30-10:30 overlaps the 09:00-10:00 default window
    assert len(gk.detect_conflict(events, "2026-11-05 09:30",
                                  "2026-11-05 10:30")) == 1


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as e:
            print(f"FAIL {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    sys.exit(1 if failed else 0)
