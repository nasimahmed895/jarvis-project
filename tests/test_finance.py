"""Tests for core/finance.py and actions/finance.py."""
from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import finance as fin
import actions.finance as fin_action


def _fresh_db():
    db = Path(tempfile.mktemp(suffix="-fin.sqlite"))
    fin.connect(db)
    fin_action._CONNECTED = True
    return db


# ── Expenses ─────────────────────────────────────────────────────────────────

def test_add_and_list_expense():
    _fresh_db()
    eid = fin.add_expense(42.5, category="food", merchant="Cafe",
                          date="2026-11-04")
    rows = fin.list_expenses(category="food")
    assert len(rows) == 1
    assert rows[0]["id"] == eid
    assert rows[0]["amount"] == 42.5


def test_delete_expense():
    _fresh_db()
    eid = fin.add_expense(10, category="misc", date="2026-11-04")
    assert fin.delete_expense(eid)
    assert fin.list_expenses() == []


def test_monthly_summary_groups_by_category():
    _fresh_db()
    fin.add_expense(10, category="food", date="2026-11-04")
    fin.add_expense(20, category="food", date="2026-11-05")
    fin.add_expense(30, category="travel", date="2026-11-10")
    fin.add_expense(5, category="food", date="2026-10-20")  # last month
    s = fin.monthly_summary(2026, 11)
    assert s["totals"]["USD"] == 60.0
    assert s["by_category"]["food"]["USD"] == 30.0
    assert s["by_category"]["travel"]["USD"] == 30.0


def test_monthly_summary_segregates_currencies():
    _fresh_db()
    fin.add_expense(10, category="food", date="2026-11-04", currency="USD")
    fin.add_expense(20, category="food", date="2026-11-04", currency="EUR")
    s = fin.monthly_summary(2026, 11)
    assert s["totals"] == {"USD": 10.0, "EUR": 20.0}
    assert s["by_category"]["food"] == {"USD": 10.0, "EUR": 20.0}


# ── Invoices ─────────────────────────────────────────────────────────────────

def test_invoice_lifecycle():
    _fresh_db()
    future = (datetime.now().date() + timedelta(days=30)).strftime("%Y-%m-%d")
    iid = fin.add_invoice("Acme", 1000.0, due_date=future)
    rows = fin.list_invoices("open")
    assert len(rows) == 1 and rows[0]["id"] == iid
    assert fin.set_invoice_status(iid, "paid")
    assert fin.list_invoices("open") == []


def test_invoice_auto_overdue():
    _fresh_db()
    past = (datetime.now().date() - timedelta(days=5)).strftime("%Y-%m-%d")
    iid = fin.add_invoice("Late", 500.0, due_date=past)
    n = fin.refresh_overdue_invoices()
    assert n == 1
    rows = fin.list_invoices("overdue")
    assert rows and rows[0]["id"] == iid


def test_invoice_status_validation():
    _fresh_db()
    iid = fin.add_invoice("C", 100)
    try:
        fin.set_invoice_status(iid, "weird")
    except ValueError:
        return
    raise AssertionError("expected ValueError")


# ── Subscriptions ────────────────────────────────────────────────────────────

def test_subscription_cost_normalises_to_monthly():
    _fresh_db()
    fin.add_subscription("Netflix", 15, period="monthly")
    fin.add_subscription("Prime", 120, period="yearly")
    fin.add_subscription("Coffee club", 5, period="weekly")
    totals = fin.monthly_subscription_cost()
    # 15 + 10 + 5*52/12 ≈ 15 + 10 + 21.67 ≈ 46.67
    assert abs(totals["USD"] - 46.67) < 0.01


def test_subscription_cancel_removes_from_active():
    _fresh_db()
    sid = fin.add_subscription("Gym", 50)
    assert fin.cancel_subscription(sid)
    assert fin.active_subscriptions() == []


def test_subscription_period_validation():
    _fresh_db()
    try:
        fin.add_subscription("bad", 5, period="hourly")
    except ValueError:
        return
    raise AssertionError("expected ValueError on bad period")


# ── Budgets ──────────────────────────────────────────────────────────────────

def test_budget_status_computes_pct():
    _fresh_db()
    fin.set_budget("food", 100)
    fin.add_expense(40, category="food", date=datetime.now().strftime("%Y-%m-%d"))
    fin.add_expense(30, category="food", date=datetime.now().strftime("%Y-%m-%d"))
    rows = fin.budget_status()
    assert rows and rows[0]["category"] == "food"
    assert rows[0]["spent"] == 70.0
    assert rows[0]["remaining"] == 30.0
    assert abs(rows[0]["pct"] - 0.7) < 0.001
    assert not rows[0]["over"]


def test_budget_status_flags_over():
    _fresh_db()
    fin.set_budget("tools", 50)
    fin.add_expense(100, category="tools",
                    date=datetime.now().strftime("%Y-%m-%d"))
    rows = fin.budget_status()
    assert rows[0]["over"]


def test_budget_upsert_overwrites():
    _fresh_db()
    fin.set_budget("food", 200)
    fin.set_budget("food", 300)  # same key → UPDATE
    rows = fin.list_budgets()
    assert len(rows) == 1 and rows[0]["amount"] == 300


# ── Bills ────────────────────────────────────────────────────────────────────

def test_bill_pay_sets_paid_flag():
    _fresh_db()
    bid = fin.add_bill("Rent", 1200,
                       due_date=datetime.now().strftime("%Y-%m-%d"))
    assert fin.pay_bill(bid)
    assert not fin.upcoming_bills(within_days=30)


def test_bill_recurring_validation():
    _fresh_db()
    try:
        fin.add_bill("x", 1, "2026-11-04", recurring="daily")
    except ValueError:
        return
    raise AssertionError("expected ValueError on bad recurring")


def test_upcoming_bills_includes_overdue():
    _fresh_db()
    past = (datetime.now().date() - timedelta(days=3)).strftime("%Y-%m-%d")
    future = (datetime.now().date() + timedelta(days=5)).strftime("%Y-%m-%d")
    fin.add_bill("Water", 50, past)
    fin.add_bill("Gas", 70, future)
    rows = fin.upcoming_bills(within_days=14, include_overdue=True)
    assert len(rows) == 2


# ── Receipts ─────────────────────────────────────────────────────────────────

def test_receipt_link():
    _fresh_db()
    eid = fin.add_expense(25, category="food",
                          date=datetime.now().strftime("%Y-%m-%d"))
    rid = fin.add_receipt(path="/tmp/r.png", parsed_text="Cafe $25")
    assert fin.link_receipt_to_expense(rid, eid)
    r = fin.get_receipt(rid)
    assert r and r["linked_expense_id"] == eid


# ── Action layer ─────────────────────────────────────────────────────────────

def test_action_add_and_list():
    _fresh_db()
    out = fin_action.finance({"action": "add_expense", "amount": 12.5,
                              "category": "food"})
    assert "Logged expense" in out
    out = fin_action.finance({"action": "list_expenses"})
    assert "[food]" in out


def test_action_rejects_bad_amount():
    _fresh_db()
    assert "numeric 'amount'" in fin_action.finance({"action": "add_expense"})


def test_action_budget_status():
    _fresh_db()
    fin_action.finance({"action": "set_budget", "category": "travel",
                        "amount": 500})
    fin_action.finance({"action": "add_expense", "amount": 300,
                        "category": "travel"})
    out = fin_action.finance({"action": "budget_status"})
    assert "travel" in out and "500" in out


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
