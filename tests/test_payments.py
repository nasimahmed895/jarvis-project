"""Tests for core/payments.py and actions/payment.py."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import payments as pay
import actions.payment as pay_action


def _fresh():
    pay.connect(Path(tempfile.mktemp(suffix="-pay.sqlite")))
    pay_action._CONNECTED = True


# ── Prepare ─────────────────────────────────────────────────────────────────

def test_prepare_local_defaults():
    _fresh()
    intent = pay.prepare(42.5, description="dinner", payee="Restaurant")
    assert intent["status"] == "prepared"
    assert intent["provider"] == "local"
    assert intent["amount"] == 42.5


def test_prepare_rejects_zero_or_negative():
    _fresh()
    for bad in (0, -1):
        try:
            pay.prepare(bad)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {bad}")


def test_prepare_refuses_live_stripe_key():
    _fresh()
    with mock.patch.object(pay, "_resolve_key", return_value="sk_live_abc123"):
        try:
            pay.prepare(10, provider="stripe_test")
        except pay.PaymentSecurityError as e:
            assert "live" in str(e).lower() or "non-test" in str(e)
            return
    raise AssertionError("expected PaymentSecurityError on live key")


def test_prepare_degrades_when_no_key():
    _fresh()
    with mock.patch.object(pay, "_resolve_key", return_value=None):
        intent = pay.prepare(10, provider="stripe_test")
    assert intent["provider"] == "local"
    assert intent["metadata"].get("degraded_from") == "stripe_test"


def test_prepare_stripe_test_key_accepted():
    _fresh()
    with mock.patch.object(pay, "_resolve_key",
                            return_value="sk_test_ABC"):
        intent = pay.prepare(10, provider="stripe_test")
    assert intent["provider"] == "stripe_test"
    assert intent["provider_id"]  # placeholder present


# ── Confirm ─────────────────────────────────────────────────────────────────

def test_confirm_requires_confirmed_by():
    _fresh()
    intent = pay.prepare(10)
    try:
        pay.confirm_intent(intent["id"], confirmed_by="")
    except pay.PaymentSecurityError:
        return
    raise AssertionError("expected PaymentSecurityError on empty proof")


def test_confirm_moves_to_confirmed_and_mirrors_finance():
    _fresh()
    intent = pay.prepare(25, payee="Cafe", description="lunch")
    # Patch finance mirror so we don't need a real finance DB.
    with mock.patch("core.finance.add_expense") as add_exp:
        out = pay.confirm_intent(intent["id"], confirmed_by="user")
    assert out["status"] == "confirmed"
    add_exp.assert_called_once()


def test_confirm_is_idempotent_on_non_prepared():
    _fresh()
    intent = pay.prepare(10)
    pay.cancel_intent(intent["id"])
    out = pay.confirm_intent(intent["id"], confirmed_by="user")
    # Cancelled state is preserved; no state regression.
    assert out["status"] == "cancelled"


def test_confirm_missing_intent_raises():
    _fresh()
    try:
        pay.confirm_intent(999, confirmed_by="user")
    except LookupError:
        return
    raise AssertionError("expected LookupError")


# ── Cancel / verify / list ──────────────────────────────────────────────────

def test_cancel_only_prepared():
    _fresh()
    intent = pay.prepare(1)
    out = pay.cancel_intent(intent["id"])
    assert out["status"] == "cancelled"


def test_verify_round_trip():
    _fresh()
    intent = pay.prepare(1, currency="eur")
    r = pay.verify_intent(intent["id"])
    assert r["ok"]
    assert r["currency"] == "EUR"
    assert r["status"] == "prepared"


def test_list_filters_by_status():
    _fresh()
    a = pay.prepare(1)
    b = pay.prepare(2)
    pay.cancel_intent(b["id"])
    prepared = pay.list_intents(status="prepared")
    cancelled = pay.list_intents(status="cancelled")
    assert {r["id"] for r in prepared} == {a["id"]}
    assert {r["id"] for r in cancelled} == {b["id"]}


# ── Action layer ────────────────────────────────────────────────────────────

def test_action_prepare_and_confirm_flow():
    _fresh()
    out = pay_action.payment({
        "action": "prepare",
        "amount": 12.5,
        "description": "test",
    })
    assert "Prepared intent" in out
    intent_id = pay.list_intents()[0]["id"]
    with mock.patch("core.finance.add_expense"):
        out = pay_action.payment({
            "action": "confirm",
            "id": intent_id,
            "confirmed_by": "user",
        })
    assert "Confirmed" in out


def test_action_confirm_requires_proof():
    _fresh()
    intent = pay.prepare(1)
    out = pay_action.payment({"action": "confirm", "id": intent["id"]})
    assert "confirmed_by" in out


def test_action_prepare_rejects_bad_amount():
    _fresh()
    assert "positive" in pay_action.payment({"action": "prepare",
                                              "amount": 0})


def test_action_live_key_refused_through_tool():
    _fresh()
    with mock.patch.object(pay, "_resolve_key", return_value="sk_live_xxx"):
        out = pay_action.payment({
            "action": "prepare",
            "amount": 5,
            "provider": "stripe_test",
        })
    assert "REFUSED" in out


def test_action_list_rejects_unknown():
    _fresh()
    assert "unknown action" in pay_action.payment({"action": "warp"})


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
