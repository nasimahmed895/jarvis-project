"""
Payment action — prepare / confirm / verify sandbox transactions.

Wraps ``core/payments.py``. Every flow is TWO explicit steps: the agent
prepares an intent (safe, local record), the user then confirms via a
separate action with a ``confirmed_by`` proof. Live keys are refused;
only sandbox / test credentials ever reach the provider path, and even
those are hooked with ``metadata.degraded_from`` when the user has no
keychain entry so the behaviour remains honest.
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any

from core import payments as _pay


_CONNECTED = False


def _ensure() -> None:
    global _CONNECTED
    if _CONNECTED:
        return
    db = Path(__file__).resolve().parent.parent / "memory" / "payments.sqlite"
    _pay.connect(db)
    _CONNECTED = True


def _int(raw, default=None):
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _num(raw, default=None):
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _fmt_intent(intent: dict) -> str:
    ccy = intent.get("currency", "USD")
    amount = float(intent.get("amount", 0))
    bits = [f"#{intent['id']} [{intent['status']}] "
            f"{ccy} {amount:,.2f} "
            f"provider={intent.get('provider','?')}"]
    if intent.get("payee"):
        bits.append(f"payee={intent['payee']}")
    if intent.get("description"):
        bits.append(f"desc={intent['description']}")
    meta = intent.get("metadata") or {}
    if meta.get("degraded_from"):
        bits.append(f"(degraded from {meta['degraded_from']})")
    if intent.get("confirmed_at"):
        when = _dt.datetime.fromtimestamp(float(intent["confirmed_at"])) \
            .strftime("%Y-%m-%d %H:%M")
        bits.append(f"confirmed_at={when}")
    return " · ".join(bits)


def payment(
    parameters:    dict | None = None,
    response: Any = None,
    player:   Any = None,
    session_memory: Any = None,
) -> str:
    """Prepare / confirm / verify sandbox payments.

    Actions:
      prepare       amount= currency=USD description= payee= provider=local|stripe_test|paypal_sandbox category=
      confirm       id= confirmed_by=<proof string, e.g. 'user'>
      verify        id=
      list          status=<open|confirmed|succeeded|failed|cancelled|all>
      cancel        id=
      history       — alias for list
    """
    _ensure()
    p = parameters or {}
    action = str(p.get("action", "")).lower().strip()

    if action == "prepare":
        amount = _num(p.get("amount"))
        if amount is None or amount <= 0:
            return "payment prepare: provide positive numeric 'amount'."
        try:
            intent = _pay.prepare(
                amount,
                currency=str(p.get("currency") or "USD"),
                description=p.get("description") or None,
                payee=p.get("payee") or None,
                provider=str(p.get("provider") or "local"),
                metadata={"category": p.get("category")} if p.get("category") else None,
            )
        except _pay.PaymentSecurityError as e:
            return f"payment prepare: REFUSED — {e}"
        except ValueError as e:
            return f"payment prepare: {e}"
        return f"Prepared intent — {_fmt_intent(intent)}"

    if action == "confirm":
        iid = _int(p.get("id"))
        confirmed_by = str(p.get("confirmed_by") or "").strip()
        if iid is None:
            return "payment confirm: provide numeric 'id'."
        if not confirmed_by:
            return ("payment confirm: provide 'confirmed_by' with a "
                    "proof string (e.g. 'user' once the HUD confirm "
                    "has fired). Never auto-confirm.")
        try:
            intent = _pay.confirm_intent(iid, confirmed_by=confirmed_by)
        except LookupError as e:
            return f"payment confirm: {e}"
        except _pay.PaymentSecurityError as e:
            return f"payment confirm: REFUSED — {e}"
        return f"Confirmed — {_fmt_intent(intent)}"

    if action == "verify":
        iid = _int(p.get("id"))
        if iid is None:
            return "payment verify: provide numeric 'id'."
        r = _pay.verify_intent(iid)
        if not r.get("ok"):
            return f"payment verify: {r.get('error','not found')}"
        return (f"#{r['id']} [{r['status']}] "
                f"{r['currency']} {float(r['amount']):,.2f} "
                f"provider={r['provider']} "
                f"provider_id={r.get('provider_id') or '-'}")

    if action in ("list", "history"):
        status = p.get("status")
        if status == "all":
            status = None
        rows = _pay.list_intents(status=status)
        if not rows:
            return "No payment intents."
        return (f"Payment intents ({len(rows)}):\n"
                + "\n".join("  " + _fmt_intent(r) for r in rows))

    if action == "cancel":
        iid = _int(p.get("id"))
        if iid is None:
            return "payment cancel: provide numeric 'id'."
        intent = _pay.cancel_intent(iid)
        if not intent:
            return f"payment cancel: #{iid} not found."
        return f"Cancelled — {_fmt_intent(intent)}"

    return (f"payment: unknown action {action!r}. "
            "Actions: prepare | confirm | verify | list | cancel | history")


TOOL = {
    "name": "payment",
    "description": (
        "Prepare / confirm / verify sandbox payments. Every transaction "
        "is a TWO-step dance: 'prepare' records an intent locally (no "
        "money moves, no network call for provider=local), then a "
        "separate 'confirm' with an explicit 'confirmed_by' proof moves "
        "it to confirmed state. LIVE payment keys are hard-refused; "
        "only Stripe test (sk_test_...) and PayPal sandbox credentials "
        "are accepted, and only when the user has deliberately stored "
        "them in the OS keychain via core.secrets. When no key is "
        "present the request degrades to a local sandbox record "
        "(labelled 'degraded_from') so the voice loop never pretends to "
        "have talked to a bank. Local-sandbox confirmations also mirror "
        "the amount into finance.add_expense."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action":       {"type": "STRING",
                             "description": "prepare | confirm | verify | list | cancel | history"},
            "id":           {"type": "INTEGER", "description": "Intent id."},
            "amount":       {"type": "NUMBER",  "description": "Amount to pay (must be > 0)."},
            "currency":     {"type": "STRING",  "description": "ISO currency code."},
            "description":  {"type": "STRING",  "description": "What the payment is for."},
            "payee":        {"type": "STRING",  "description": "Who's being paid."},
            "provider":     {"type": "STRING",  "description": "local | stripe_test | paypal_sandbox"},
            "category":     {"type": "STRING",  "description": "Expense category (for finance mirror)."},
            "confirmed_by": {"type": "STRING",  "description": "Explicit confirmation proof (e.g. 'user')."},
            "status":       {"type": "STRING",  "description": "Filter for 'list' (prepared | confirmed | succeeded | failed | cancelled | all)."},
        },
        "required": ["action"],
    },
    "handler": payment,
}
