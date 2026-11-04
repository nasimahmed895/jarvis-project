"""
Finance action — expenses, invoices, subscriptions, budgets, bills.

Thin wrapper over ``core/finance.py``. Everything is a local DB write;
no payment execution, no external APIs. The confirm gate still catches
anything the user explicitly wants protected via the trust-me flow.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from core import finance as _fin

_CONNECTED = False


def _ensure() -> None:
    global _CONNECTED
    if _CONNECTED:
        return
    db = Path(__file__).resolve().parent.parent / "memory" / "finance.sqlite"
    _fin.connect(db)
    _CONNECTED = True


def _num(raw, default=None):
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _int(raw, default=None):
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _fmt_money(amount, currency):
    return f"{currency} {amount:,.2f}"


def finance(
    parameters:    dict | None = None,
    response: Any = None,
    player:   Any = None,
    session_memory: Any = None,
) -> str:
    """Manage personal finance: expenses, invoices, subscriptions,
    budgets, bills, receipts. Local DB only — nothing sends money.

    Actions:
      add_expense        amount= category= merchant= date= currency= notes=
      list_expenses      category= since= until= limit=
      delete_expense     id=
      monthly_summary    year= month=
      add_invoice        client= amount= number= issue_date= due_date= currency= notes=
      invoice_status     id= status=<open|paid|overdue|cancelled>
      list_invoices      status=
      add_subscription   name= amount= period=<weekly|monthly|yearly>
                         next_charge= currency= notes=
      cancel_subscription id=
      list_subscriptions
      subscription_cost  — monthly cost of active subs per currency
      set_budget         category= amount= period=monthly currency=
      list_budgets
      budget_status      year= month=
      add_bill           name= amount= due_date= currency= recurring= notes=
      pay_bill           id=
      upcoming_bills     within_days= include_overdue=0|1
      add_receipt        path= parsed_text= linked_expense_id=
      link_receipt       receipt_id= expense_id=
    """
    _ensure()
    p = parameters or {}
    action = str(p.get("action", "")).lower().strip()

    if action == "add_expense":
        amt = _num(p.get("amount"))
        if amt is None:
            return "finance add_expense: provide numeric 'amount'."
        eid = _fin.add_expense(
            amt,
            category=str(p.get("category") or "misc"),
            merchant=p.get("merchant") or None,
            date=p.get("date") or None,
            currency=str(p.get("currency") or "USD"),
            notes=p.get("notes") or None,
        )
        return f"Logged expense #{eid}: {_fmt_money(amt, (p.get('currency') or 'USD').upper())}."

    if action == "list_expenses":
        rows = _fin.list_expenses(
            category=p.get("category") or None,
            since=p.get("since") or None,
            until=p.get("until") or None,
            limit=_int(p.get("limit"), 50) or 50,
        )
        if not rows:
            return "No expenses matching."
        lines = [f"Expenses ({len(rows)}):"]
        for r in rows:
            m = f" @ {r['merchant']}" if r.get("merchant") else ""
            lines.append(f"  #{r['id']} {r['date']} [{r['category']}] "
                         f"{_fmt_money(float(r['amount']), r['currency'])}{m}")
        return "\n".join(lines)

    if action == "delete_expense":
        eid = _int(p.get("id"))
        if eid is None:
            return "finance delete_expense: provide numeric 'id'."
        return (f"Deleted expense #{eid}." if _fin.delete_expense(eid)
                else f"Expense #{eid} not found.")

    if action == "monthly_summary":
        s = _fin.monthly_summary(
            year=_int(p.get("year")),
            month=_int(p.get("month")),
        )
        lines = [f"Summary for {s['year']}-{s['month']:02d} "
                 f"({s['count']} expense(s)):"]
        for ccy, total in s["totals"].items():
            lines.append(f"  Total {_fmt_money(total, ccy)}")
        for cat, by_ccy in s["by_category"].items():
            parts = ", ".join(_fmt_money(a, c) for c, a in by_ccy.items())
            lines.append(f"  {cat}: {parts}")
        return "\n".join(lines)

    if action == "add_invoice":
        client = str(p.get("client") or p.get("client_name") or "").strip()
        amt = _num(p.get("amount"))
        if not client or amt is None:
            return "finance add_invoice: provide 'client' and numeric 'amount'."
        iid = _fin.add_invoice(
            client, amt,
            number=p.get("number") or None,
            issue_date=p.get("issue_date") or None,
            due_date=p.get("due_date") or None,
            currency=str(p.get("currency") or "USD"),
            notes=p.get("notes") or None,
        )
        return f"Invoice #{iid} for {client}: {_fmt_money(amt, (p.get('currency') or 'USD').upper())}."

    if action == "invoice_status":
        iid = _int(p.get("id"))
        status = str(p.get("status") or "").lower()
        if iid is None or status not in ("open", "paid", "overdue", "cancelled"):
            return "finance invoice_status: provide 'id' and status in {open,paid,overdue,cancelled}."
        return (f"Invoice #{iid} → {status}."
                if _fin.set_invoice_status(iid, status)
                else f"Invoice #{iid} not found.")

    if action == "list_invoices":
        status = p.get("status") or None
        rows = _fin.list_invoices(status=status)
        if not rows:
            return "No invoices."
        lines = [f"Invoices ({len(rows)}):"]
        for r in rows:
            due = f" due {r['due_date']}" if r.get("due_date") else ""
            lines.append(f"  #{r['id']} [{r['status']}] {r['client_name']} "
                         f"{_fmt_money(float(r['amount']), r['currency'])}{due}")
        return "\n".join(lines)

    if action == "add_subscription":
        name = str(p.get("name") or "").strip()
        amt = _num(p.get("amount"))
        if not name or amt is None:
            return "finance add_subscription: provide 'name' and numeric 'amount'."
        try:
            sid = _fin.add_subscription(
                name, amt,
                period=str(p.get("period") or "monthly"),
                next_charge=p.get("next_charge") or None,
                currency=str(p.get("currency") or "USD"),
                notes=p.get("notes") or None,
            )
        except ValueError as e:
            return f"finance add_subscription: {e}"
        return f"Subscription #{sid}: {name} — {_fmt_money(amt, (p.get('currency') or 'USD').upper())}/{p.get('period') or 'monthly'}."

    if action == "cancel_subscription":
        sid = _int(p.get("id"))
        if sid is None:
            return "finance cancel_subscription: provide numeric 'id'."
        return (f"Cancelled subscription #{sid}."
                if _fin.cancel_subscription(sid)
                else f"Subscription #{sid} not found or already inactive.")

    if action == "list_subscriptions":
        rows = _fin.active_subscriptions()
        if not rows:
            return "No active subscriptions."
        lines = [f"Active subscriptions ({len(rows)}):"]
        for r in rows:
            nxt = f" next {r['next_charge']}" if r.get("next_charge") else ""
            lines.append(f"  #{r['id']} {r['name']} "
                         f"{_fmt_money(float(r['amount']), r['currency'])}/{r['period']}{nxt}")
        return "\n".join(lines)

    if action == "subscription_cost":
        totals = _fin.monthly_subscription_cost()
        if not totals:
            return "No active subscriptions."
        return "Monthly subscription cost:\n" + "\n".join(
            f"  {_fmt_money(v, c)}" for c, v in totals.items()
        )

    if action == "set_budget":
        cat = str(p.get("category") or "").lower().strip()
        amt = _num(p.get("amount"))
        if not cat or amt is None:
            return "finance set_budget: provide 'category' and numeric 'amount'."
        bid = _fin.set_budget(
            cat, amt,
            period=str(p.get("period") or "monthly"),
            currency=str(p.get("currency") or "USD"),
        )
        return f"Budget set #{bid}: {cat} {_fmt_money(amt, (p.get('currency') or 'USD').upper())}/{p.get('period') or 'monthly'}."

    if action == "list_budgets":
        rows = _fin.list_budgets()
        if not rows:
            return "No budgets configured."
        lines = [f"Budgets ({len(rows)}):"]
        for r in rows:
            lines.append(f"  {r['category']}: "
                         f"{_fmt_money(float(r['amount']), r['currency'])}/{r['period']}")
        return "\n".join(lines)

    if action == "budget_status":
        rows = _fin.budget_status(
            year=_int(p.get("year")),
            month=_int(p.get("month")),
        )
        if not rows:
            return "No budgets to compare."
        lines = ["Budget status:"]
        for r in rows:
            flag = "OVER" if r["over"] else "ok"
            lines.append(f"  [{flag}] {r['category']} ({r['currency']}): "
                         f"{_fmt_money(r['spent'], r['currency'])} / "
                         f"{_fmt_money(r['budget'], r['currency'])} "
                         f"({r['pct']*100:.0f}%)")
        return "\n".join(lines)

    if action == "add_bill":
        name = str(p.get("name") or "").strip()
        amt = _num(p.get("amount"))
        due = str(p.get("due_date") or "").strip()
        if not name or amt is None or not due:
            return "finance add_bill: provide 'name', numeric 'amount', 'due_date'."
        try:
            bid = _fin.add_bill(
                name, amt, due,
                currency=str(p.get("currency") or "USD"),
                recurring=p.get("recurring") or None,
                notes=p.get("notes") or None,
            )
        except ValueError as e:
            return f"finance add_bill: {e}"
        return f"Bill #{bid}: {name} — {_fmt_money(amt, (p.get('currency') or 'USD').upper())} due {due}."

    if action == "pay_bill":
        bid = _int(p.get("id"))
        if bid is None:
            return "finance pay_bill: provide numeric 'id'."
        return (f"Marked bill #{bid} paid."
                if _fin.pay_bill(bid)
                else f"Bill #{bid} not found or already paid.")

    if action == "upcoming_bills":
        days = _int(p.get("within_days"), 14) or 14
        include_overdue = bool(_int(p.get("include_overdue"), 1))
        rows = _fin.upcoming_bills(within_days=days,
                                    include_overdue=include_overdue)
        if not rows:
            return f"No bills due within {days} day(s)."
        lines = [f"Upcoming bills (next {days}d, {len(rows)}):"]
        for r in rows:
            lines.append(f"  #{r['id']} {r['due_date']} {r['name']} "
                         f"{_fmt_money(float(r['amount']), r['currency'])}")
        return "\n".join(lines)

    if action == "add_receipt":
        rid = _fin.add_receipt(
            path=p.get("path") or None,
            parsed_text=p.get("parsed_text") or None,
            linked_expense_id=_int(p.get("linked_expense_id")),
        )
        return f"Receipt #{rid} stored."

    if action == "link_receipt":
        rid = _int(p.get("receipt_id"))
        eid = _int(p.get("expense_id"))
        if rid is None or eid is None:
            return "finance link_receipt: provide 'receipt_id' and 'expense_id'."
        return (f"Linked receipt #{rid} ↔ expense #{eid}."
                if _fin.link_receipt_to_expense(rid, eid)
                else "Link failed (ids not found).")

    return (f"finance: unknown action {action!r}. "
            "Actions: add_expense | list_expenses | delete_expense | "
            "monthly_summary | add_invoice | invoice_status | list_invoices "
            "| add_subscription | cancel_subscription | list_subscriptions "
            "| subscription_cost | set_budget | list_budgets | "
            "budget_status | add_bill | pay_bill | upcoming_bills | "
            "add_receipt | link_receipt")


TOOL = {
    "name": "finance",
    "description": (
        "Personal finance manager: expenses, invoices, subscriptions, "
        "budgets, bills, receipts. Local DB only — JARVIS never moves "
        "money. Use 'add_expense' every time the user mentions a "
        "purchase ('I spent $40 on lunch'). Use 'add_subscription' when "
        "they name a recurring service. Use 'add_bill' for one-off "
        "payments with a due date. Use 'set_budget' then 'budget_status' "
        "to answer 'am I under budget this month?'. Use 'monthly_summary' "
        "for a spending breakdown. Multi-currency: amounts stay in their "
        "declared currency; no conversion is done."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action":            {"type": "STRING", "description": "see docstring"},
            "id":                {"type": "INTEGER", "description": "Row id (expense/invoice/subscription/bill/receipt)."},
            "amount":            {"type": "NUMBER", "description": "Amount value."},
            "currency":          {"type": "STRING", "description": "ISO currency code (default USD)."},
            "category":          {"type": "STRING", "description": "Expense category (food, travel, tools…)."},
            "merchant":          {"type": "STRING", "description": "Merchant or payee."},
            "date":              {"type": "STRING", "description": "YYYY-MM-DD."},
            "since":             {"type": "STRING", "description": "Filter: inclusive start YYYY-MM-DD."},
            "until":             {"type": "STRING", "description": "Filter: inclusive end YYYY-MM-DD."},
            "year":              {"type": "INTEGER", "description": "Summary / budget year."},
            "month":              {"type": "INTEGER", "description": "Summary / budget month (1-12)."},
            "limit":             {"type": "INTEGER", "description": "Row cap for listings."},
            "client":            {"type": "STRING", "description": "Invoice client name."},
            "number":            {"type": "STRING", "description": "Invoice number."},
            "issue_date":        {"type": "STRING", "description": "Invoice issue date YYYY-MM-DD."},
            "due_date":          {"type": "STRING", "description": "Due date YYYY-MM-DD."},
            "status":            {"type": "STRING", "description": "open | paid | overdue | cancelled"},
            "name":              {"type": "STRING", "description": "Subscription or bill name."},
            "period":            {"type": "STRING", "description": "weekly | monthly | yearly"},
            "next_charge":       {"type": "STRING", "description": "YYYY-MM-DD for subscription next charge."},
            "recurring":         {"type": "STRING", "description": "Bill recurrence: monthly | yearly | (omit)."},
            "within_days":       {"type": "INTEGER", "description": "Horizon for upcoming_bills (default 14)."},
            "include_overdue":   {"type": "INTEGER", "description": "upcoming_bills: 1 includes overdue (default)."},
            "notes":             {"type": "STRING", "description": "Freeform notes."},
            "path":              {"type": "STRING", "description": "Receipt file path."},
            "parsed_text":       {"type": "STRING", "description": "Receipt OCR text."},
            "linked_expense_id": {"type": "INTEGER", "description": "Expense to link a receipt to on create."},
            "receipt_id":        {"type": "INTEGER", "description": "Receipt id for link_receipt."},
            "expense_id":        {"type": "INTEGER", "description": "Expense id for link_receipt."},
        },
        "required": ["action"],
    },
    "handler": finance,
}
