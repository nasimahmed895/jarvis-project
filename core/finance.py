"""
Personal finance store.

The audit flagged section 9 (Finance & Administration) at 0/8. This module
is the foundation for every section-9 feature: expenses, invoices,
subscriptions, budgets, bills, and receipts. Lives at
``memory/finance.sqlite``. Deliberately small and opinionated — a full
accounting system is out of scope; this is just enough state for JARVIS
to answer "how much did I spend on travel last month?" and
"what subscriptions renew this week?".

No external API, no bank sync, no payment execution. Writes are local;
the voice layer still refuses irreversible financial actions at the
confirm gate.
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
        CREATE TABLE IF NOT EXISTS expenses (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            amount      REAL    NOT NULL,
            currency    TEXT    NOT NULL DEFAULT 'USD',
            category    TEXT    NOT NULL DEFAULT 'misc',
            merchant    TEXT    DEFAULT NULL,
            date        TEXT    NOT NULL,          -- YYYY-MM-DD
            notes       TEXT    DEFAULT NULL,
            receipt_id  INTEGER DEFAULT NULL,
            created_at  REAL    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_exp_date     ON expenses(date);
        CREATE INDEX IF NOT EXISTS idx_exp_category ON expenses(category);

        CREATE TABLE IF NOT EXISTS invoices (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            number      TEXT    DEFAULT NULL,
            client_name TEXT    NOT NULL,
            amount      REAL    NOT NULL,
            currency    TEXT    NOT NULL DEFAULT 'USD',
            issue_date  TEXT    NOT NULL,          -- YYYY-MM-DD
            due_date    TEXT    DEFAULT NULL,
            status      TEXT    NOT NULL DEFAULT 'open',  -- open | paid | overdue | cancelled
            notes       TEXT    DEFAULT NULL,
            created_at  REAL    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_inv_status ON invoices(status);
        CREATE INDEX IF NOT EXISTS idx_inv_due    ON invoices(due_date);

        CREATE TABLE IF NOT EXISTS subscriptions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT    NOT NULL,
            amount      REAL    NOT NULL,
            currency    TEXT    NOT NULL DEFAULT 'USD',
            period      TEXT    NOT NULL DEFAULT 'monthly', -- weekly | monthly | yearly
            next_charge TEXT    DEFAULT NULL,       -- YYYY-MM-DD
            active      INTEGER NOT NULL DEFAULT 1,
            category    TEXT    DEFAULT 'subscriptions',
            notes       TEXT    DEFAULT NULL,
            created_at  REAL    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_sub_active ON subscriptions(active);
        CREATE INDEX IF NOT EXISTS idx_sub_next   ON subscriptions(next_charge);

        CREATE TABLE IF NOT EXISTS budgets (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            category    TEXT    NOT NULL,
            period      TEXT    NOT NULL DEFAULT 'monthly',
            amount      REAL    NOT NULL,
            currency    TEXT    NOT NULL DEFAULT 'USD',
            created_at  REAL    NOT NULL,
            UNIQUE(category, period)
        );

        CREATE TABLE IF NOT EXISTS bills (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT    NOT NULL,
            amount      REAL    NOT NULL,
            currency    TEXT    NOT NULL DEFAULT 'USD',
            due_date    TEXT    NOT NULL,            -- YYYY-MM-DD
            paid        INTEGER NOT NULL DEFAULT 0,
            recurring   TEXT    DEFAULT NULL,        -- 'monthly' | 'yearly' | NULL
            notes       TEXT    DEFAULT NULL,
            created_at  REAL    NOT NULL,
            paid_at     REAL    DEFAULT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_bills_due  ON bills(due_date);
        CREATE INDEX IF NOT EXISTS idx_bills_paid ON bills(paid);

        CREATE TABLE IF NOT EXISTS receipts (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            path               TEXT    DEFAULT NULL,
            parsed_text        TEXT    DEFAULT NULL,
            linked_expense_id  INTEGER DEFAULT NULL,
            created_at         REAL    NOT NULL,
            FOREIGN KEY (linked_expense_id) REFERENCES expenses(id) ON DELETE SET NULL
        );
        """
    )
    _DB.commit()


def _now() -> float:
    return time.time()


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _month_range(year: int, month: int) -> tuple[str, str]:
    start = datetime(year, month, 1)
    end = (datetime(year + 1, 1, 1) if month == 12
           else datetime(year, month + 1, 1))
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


# ── Expenses ─────────────────────────────────────────────────────────────────

def add_expense(amount: float, *, category: str = "misc",
                merchant: str | None = None, date: str | None = None,
                currency: str = "USD", notes: str | None = None,
                receipt_id: int | None = None) -> int:
    date = date or _today()
    with _LOCK:
        cur = _DB.execute(
            "INSERT INTO expenses(amount, currency, category, merchant, "
            "date, notes, receipt_id, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (float(amount), currency.upper(), category.lower(), merchant,
             date, notes, receipt_id, _now()),
        )
        _DB.commit()
        return int(cur.lastrowid)


def list_expenses(*, category: str | None = None,
                  since: str | None = None, until: str | None = None,
                  limit: int = 50) -> list[dict]:
    with _LOCK:
        q = "SELECT * FROM expenses WHERE 1=1"
        args: list = []
        if category:
            q += " AND category=?"
            args.append(category.lower())
        if since:
            q += " AND date>=?"
            args.append(since)
        if until:
            q += " AND date<=?"
            args.append(until)
        q += " ORDER BY date DESC, id DESC LIMIT ?"
        args.append(limit)
        rows = _DB.execute(q, args).fetchall()
    return [dict(r) for r in rows]


def delete_expense(expense_id: int) -> bool:
    with _LOCK:
        res = _DB.execute("DELETE FROM expenses WHERE id=?", (expense_id,))
        _DB.commit()
    return bool(res.rowcount)


def monthly_summary(year: int | None = None,
                    month: int | None = None) -> dict:
    """Totals by category + grand total for a month. Defaults to the
    current month. Currencies are not converted — totals are reported per
    currency so the caller can show the USD line separately from the EUR
    line without pretending they're interchangeable."""
    if year is None or month is None:
        now = datetime.now()
        year = year or now.year
        month = month or now.month
    start, end = _month_range(year, month)
    with _LOCK:
        rows = _DB.execute(
            "SELECT category, currency, SUM(amount) AS total, COUNT(*) AS n "
            "FROM expenses WHERE date>=? AND date<? "
            "GROUP BY category, currency ORDER BY total DESC",
            (start, end),
        ).fetchall()
    by_cat: dict = {}
    totals: dict[str, float] = {}
    for r in rows:
        cat = r["category"]
        ccy = r["currency"]
        by_cat.setdefault(cat, {})[ccy] = round(float(r["total"]), 2)
        totals[ccy] = round(totals.get(ccy, 0.0) + float(r["total"]), 2)
    return {
        "year": year, "month": month,
        "by_category": by_cat,
        "totals":      totals,
        "count":       sum(int(r["n"]) for r in rows),
    }


# ── Invoices ─────────────────────────────────────────────────────────────────

def add_invoice(client_name: str, amount: float, *,
                number: str | None = None,
                issue_date: str | None = None,
                due_date: str | None = None,
                currency: str = "USD",
                notes: str | None = None) -> int:
    issue_date = issue_date or _today()
    with _LOCK:
        cur = _DB.execute(
            "INSERT INTO invoices(number, client_name, amount, currency, "
            "issue_date, due_date, notes, created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (number, client_name, float(amount), currency.upper(),
             issue_date, due_date, notes, _now()),
        )
        _DB.commit()
        return int(cur.lastrowid)


def set_invoice_status(invoice_id: int, status: str) -> bool:
    status = status.lower()
    if status not in ("open", "paid", "overdue", "cancelled"):
        raise ValueError("invalid invoice status")
    with _LOCK:
        res = _DB.execute(
            "UPDATE invoices SET status=? WHERE id=?",
            (status, invoice_id),
        )
        _DB.commit()
    return bool(res.rowcount)


def refresh_overdue_invoices() -> int:
    """Mark open invoices whose due_date is in the past as overdue."""
    today = _today()
    with _LOCK:
        res = _DB.execute(
            "UPDATE invoices SET status='overdue' "
            "WHERE status='open' AND due_date IS NOT NULL AND due_date<?",
            (today,),
        )
        _DB.commit()
    return int(res.rowcount or 0)


def list_invoices(status: str | Iterable[str] | None = None,
                  limit: int = 50) -> list[dict]:
    refresh_overdue_invoices()
    with _LOCK:
        if status is None:
            rows = _DB.execute(
                "SELECT * FROM invoices ORDER BY issue_date DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            if isinstance(status, str):
                status = [status]
            ph = ",".join("?" * len(list(status)))
            rows = _DB.execute(
                f"SELECT * FROM invoices WHERE status IN ({ph}) "
                f"ORDER BY due_date, id DESC LIMIT ?",
                (*status, limit),
            ).fetchall()
    return [dict(r) for r in rows]


# ── Subscriptions ────────────────────────────────────────────────────────────

def add_subscription(name: str, amount: float, *,
                     period: str = "monthly",
                     next_charge: str | None = None,
                     currency: str = "USD",
                     notes: str | None = None) -> int:
    period = period.lower()
    if period not in ("weekly", "monthly", "yearly"):
        raise ValueError("period must be weekly | monthly | yearly")
    with _LOCK:
        cur = _DB.execute(
            "INSERT INTO subscriptions(name, amount, currency, period, "
            "next_charge, notes, created_at) VALUES(?,?,?,?,?,?,?)",
            (name, float(amount), currency.upper(), period,
             next_charge, notes, _now()),
        )
        _DB.commit()
        return int(cur.lastrowid)


def cancel_subscription(sub_id: int) -> bool:
    with _LOCK:
        res = _DB.execute(
            "UPDATE subscriptions SET active=0 WHERE id=?", (sub_id,)
        )
        _DB.commit()
    return bool(res.rowcount)


def active_subscriptions() -> list[dict]:
    with _LOCK:
        rows = _DB.execute(
            "SELECT * FROM subscriptions WHERE active=1 "
            "ORDER BY next_charge"
        ).fetchall()
    return [dict(r) for r in rows]


def monthly_subscription_cost() -> dict[str, float]:
    """Sum of active subscriptions, normalised to monthly per currency."""
    totals: dict[str, float] = {}
    for s in active_subscriptions():
        amt = float(s["amount"])
        if s["period"] == "weekly":
            amt *= 52 / 12.0
        elif s["period"] == "yearly":
            amt /= 12.0
        totals[s["currency"]] = round(
            totals.get(s["currency"], 0.0) + amt, 2
        )
    return totals


# ── Budgets ──────────────────────────────────────────────────────────────────

def set_budget(category: str, amount: float, *,
               period: str = "monthly",
               currency: str = "USD") -> int:
    with _LOCK:
        _DB.execute(
            "INSERT OR REPLACE INTO budgets(category, period, amount, "
            "currency, created_at) VALUES(?,?,?,?,?)",
            (category.lower(), period.lower(), float(amount),
             currency.upper(), _now()),
        )
        _DB.commit()
        row = _DB.execute(
            "SELECT id FROM budgets WHERE category=? AND period=?",
            (category.lower(), period.lower()),
        ).fetchone()
    return int(row["id"]) if row else 0


def list_budgets() -> list[dict]:
    with _LOCK:
        rows = _DB.execute(
            "SELECT * FROM budgets ORDER BY category"
        ).fetchall()
    return [dict(r) for r in rows]


def budget_status(year: int | None = None,
                  month: int | None = None) -> list[dict]:
    """For each monthly budget, compare budget vs this month's spend in
    the same currency. Returns [{category, currency, budget, spent, remaining, pct}]."""
    s = monthly_summary(year, month)
    budgets = [b for b in list_budgets() if b["period"] == "monthly"]
    out = []
    for b in budgets:
        cat = b["category"]
        ccy = b["currency"]
        spent = (s["by_category"].get(cat, {}) or {}).get(ccy, 0.0)
        remaining = round(float(b["amount"]) - spent, 2)
        pct = (spent / float(b["amount"])) if b["amount"] else 0.0
        out.append({
            "category":  cat,
            "currency":  ccy,
            "budget":    round(float(b["amount"]), 2),
            "spent":     round(spent, 2),
            "remaining": remaining,
            "pct":       round(pct, 3),
            "over":      pct >= 1.0,
        })
    return out


# ── Bills ────────────────────────────────────────────────────────────────────

def add_bill(name: str, amount: float, due_date: str, *,
             currency: str = "USD",
             recurring: str | None = None,
             notes: str | None = None) -> int:
    if recurring and recurring.lower() not in ("monthly", "yearly"):
        raise ValueError("recurring must be 'monthly' or 'yearly'")
    with _LOCK:
        cur = _DB.execute(
            "INSERT INTO bills(name, amount, currency, due_date, recurring, "
            "notes, created_at) VALUES(?,?,?,?,?,?,?)",
            (name, float(amount), currency.upper(), due_date,
             recurring.lower() if recurring else None, notes, _now()),
        )
        _DB.commit()
        return int(cur.lastrowid)


def pay_bill(bill_id: int) -> bool:
    with _LOCK:
        res = _DB.execute(
            "UPDATE bills SET paid=1, paid_at=? WHERE id=? AND paid=0",
            (_now(), bill_id),
        )
        _DB.commit()
    return bool(res.rowcount)


def upcoming_bills(within_days: int = 14,
                   include_overdue: bool = True) -> list[dict]:
    today = datetime.now().date()
    horizon = (today + timedelta(days=within_days)).strftime("%Y-%m-%d")
    today_s = today.strftime("%Y-%m-%d")
    with _LOCK:
        if include_overdue:
            rows = _DB.execute(
                "SELECT * FROM bills WHERE paid=0 AND due_date<=? "
                "ORDER BY due_date",
                (horizon,),
            ).fetchall()
        else:
            rows = _DB.execute(
                "SELECT * FROM bills WHERE paid=0 AND due_date>=? AND due_date<=? "
                "ORDER BY due_date",
                (today_s, horizon),
            ).fetchall()
    return [dict(r) for r in rows]


# ── Receipts ─────────────────────────────────────────────────────────────────

def add_receipt(*, path: str | None = None,
                parsed_text: str | None = None,
                linked_expense_id: int | None = None) -> int:
    with _LOCK:
        cur = _DB.execute(
            "INSERT INTO receipts(path, parsed_text, linked_expense_id, "
            "created_at) VALUES(?,?,?,?)",
            (path, parsed_text, linked_expense_id, _now()),
        )
        _DB.commit()
        return int(cur.lastrowid)


def get_receipt(receipt_id: int) -> dict | None:
    with _LOCK:
        row = _DB.execute(
            "SELECT * FROM receipts WHERE id=?", (receipt_id,)
        ).fetchone()
    return dict(row) if row else None


def link_receipt_to_expense(receipt_id: int, expense_id: int) -> bool:
    with _LOCK:
        res = _DB.execute(
            "UPDATE receipts SET linked_expense_id=? WHERE id=?",
            (expense_id, receipt_id),
        )
        _DB.execute(
            "UPDATE expenses SET receipt_id=? WHERE id=?",
            (receipt_id, expense_id),
        )
        _DB.commit()
    return bool(res.rowcount)
