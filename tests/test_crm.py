"""Tests for core/crm.py + actions/crm.py.

Each test runs against an in-memory sqlite (actually a tempfile since we
share WAL+same_thread settings) so nothing touches the real memory/ dir.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import crm
import actions.crm as crm_action


def _fresh_db() -> Path:
    """New sqlite file per test; CRM module caches the connection by path."""
    p = Path(tempfile.mktemp(suffix="-crm.sqlite"))
    crm.connect(p)
    return p


def _rewire_action(db: Path) -> None:
    """Point actions/crm.py at the test DB without reloading the module."""
    crm.connect(db)
    crm_action._CONNECTED = True


def test_add_and_get_contact():
    _fresh_db()
    cid = crm.add_contact("Sarah Chen", email="sarah@acme.com",
                          tier="investor", org="Acme")
    c = crm.get_contact(cid)
    assert c is not None
    assert c["name"] == "Sarah Chen"
    assert c["tier"] == "investor"
    assert c["email"] == "sarah@acme.com"


def test_tier_normalisation():
    _fresh_db()
    cid = crm.add_contact("X", tier="FAMOUS")  # unknown tier
    assert crm.get_contact(cid)["tier"] == "regular"
    crm.set_tier(cid, "VIP")
    assert crm.get_contact(cid)["tier"] == "vip"


def test_find_by_email_case_insensitive():
    _fresh_db()
    crm.add_contact("Mike", email="MIKE@company.io")
    hit = crm.find_by_email("mike@COMPANY.io")
    assert hit and hit["name"] == "Mike"


def test_log_interaction_updates_last_seen():
    _fresh_db()
    cid = crm.add_contact("Alice")
    assert crm.get_contact(cid)["last_seen"] is None
    iid = crm.log_interaction(cid, "email_in", subject="hi", summary="greet")
    assert iid > 0
    c = crm.get_contact(cid)
    assert c["last_seen"] is not None
    assert (time.time() - float(c["last_seen"])) < 2.0


def test_recent_interactions_sorted_desc():
    _fresh_db()
    cid = crm.add_contact("A")
    now = time.time()
    crm.log_interaction(cid, "meeting", ts=now - 100)
    crm.log_interaction(cid, "email_in", ts=now - 10)
    crm.log_interaction(cid, "call", ts=now)
    rows = crm.recent_interactions(cid, limit=5)
    assert [r["kind"] for r in rows] == ["call", "email_in", "meeting"]


def test_follow_up_lifecycle():
    _fresh_db()
    cid = crm.add_contact("Bob")
    fid = crm.add_follow_up("Reply by Friday", contact_id=cid,
                            due_ts=time.time() + 3600 * 24)
    due = crm.due_follow_ups(within_hours=48)
    assert any(f["id"] == fid for f in due)
    assert crm.resolve_follow_up(fid)
    assert not any(f["id"] == fid
                   for f in crm.due_follow_ups(within_hours=48))


def test_search_prioritises_vip():
    _fresh_db()
    crm.add_contact("Jack Normal", email="jack@n.com", tier="regular")
    crm.add_contact("Jack VIP", email="jack@v.com", tier="vip")
    rows = crm.search_contacts("jack")
    assert len(rows) == 2
    assert rows[0]["name"] == "Jack VIP"


def test_tier_rank_monotonic():
    assert crm.tier_rank("vip") < crm.tier_rank("regular")
    assert crm.tier_rank("UNKNOWN") >= crm.tier_rank("regular")


def test_action_add_and_list():
    db = _fresh_db()
    _rewire_action(db)
    out = crm_action.crm({"action": "add_contact",
                          "name": "Jane", "tier": "client"})
    assert out.startswith("Added contact")
    out = crm_action.crm({"action": "list_contacts"})
    assert "Jane" in out
    assert "[client]" in out


def test_action_search_and_log():
    db = _fresh_db()
    _rewire_action(db)
    crm_action.crm({"action": "add_contact", "name": "Omar",
                    "email": "o@o.com"})
    hit = crm_action.crm({"action": "search", "query": "mar"})
    assert "Omar" in hit
    # Parse out the id from the listing to log an interaction
    first = crm.search_contacts("Omar")[0]
    out = crm_action.crm({"action": "log", "id": first["id"],
                          "kind": "email_in", "summary": "ping"})
    assert "Logged interaction" in out
    rec = crm_action.crm({"action": "recent", "id": first["id"]})
    assert "email_in" in rec


def test_action_follow_up_flow():
    db = _fresh_db()
    _rewire_action(db)
    crm_action.crm({"action": "add_contact", "name": "K"})
    cid = crm.search_contacts("K")[0]["id"]
    added = crm_action.crm({
        "action": "add_follow_up",
        "text": "send proposal",
        "id": cid,
        "due": "2099-01-01",
    })
    assert "Added follow-up" in added
    listed = crm_action.crm({"action": "list_follow_ups"})
    assert "send proposal" in listed


def test_action_bad_input_returns_message():
    db = _fresh_db()
    _rewire_action(db)
    assert "provide 'name'" in crm_action.crm({"action": "add_contact"})
    assert "provide numeric" in crm_action.crm({"action": "set_tier"})
    assert "unknown action" in crm_action.crm({"action": "blah"})


if __name__ == "__main__":
    # Lightweight runner so this file can be executed directly as a smoke test
    # without a pytest install on the box.
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
