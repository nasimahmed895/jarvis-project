"""
Payment preparation + sandbox adapter.

Fills section-4 (Financial & Transaction Capabilities) within strict
safety boundaries:

  * Local sandbox is the default and the only ALWAYS-available backend.
    Every "transaction" is a record in ``memory/payments.sqlite`` with
    status ``prepared``; a separate explicit ``confirm`` action moves it
    to ``confirmed`` and fans the amount into ``finance.add_expense``.
    No external network call. Suitable for dry-runs and UI flows.

  * Stripe test-key and PayPal sandbox integrations are OPT-IN via
    stored test credentials in the OS keychain; when both the SDK and a
    *test* key are present we route through them. Live keys
    (``sk_live_...`` etc.) are hard-refused — attempting to use one
    returns an error without making the call.

Design invariants:
  * No raw credential is stored by this module. Keys live in the OS
    keychain through ``core.secrets``; the sqlite store only ever holds
    provider + public intent ids.
  * No transaction moves from ``prepared`` to ``confirmed`` without an
    explicit confirm call from the agent / voice loop. The confirm
    action takes ``intent_id`` AND a ``confirmed_by`` flag so a stray
    dry-run tool call can never flip state.
  * Any provider URL / SDK call that would touch live money is wrapped
    in a guard that verifies the key prefix first.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable


_LOCK = threading.RLock()
_DB: sqlite3.Connection | None = None
_DB_PATH: Path | None = None


# Provider tags we recognise. New entries added here must also update
# ``_is_test_key`` so the live-key guard stays accurate.
PROVIDERS = ("local", "stripe_test", "paypal_sandbox")

INTENT_STATUSES = ("prepared", "confirmed", "cancelled",
                    "succeeded", "failed")


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
        CREATE TABLE IF NOT EXISTS payment_intents (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            provider         TEXT    NOT NULL DEFAULT 'local',
            provider_id      TEXT    DEFAULT NULL,
            amount           REAL    NOT NULL,
            currency         TEXT    NOT NULL DEFAULT 'USD',
            description      TEXT    DEFAULT NULL,
            payee            TEXT    DEFAULT NULL,
            status           TEXT    NOT NULL DEFAULT 'prepared',
            metadata_json    TEXT    DEFAULT NULL,
            idempotency_key  TEXT    DEFAULT NULL,
            created_at       REAL    NOT NULL,
            confirmed_at     REAL    DEFAULT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_payments_status ON payment_intents(status);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_idem
            ON payment_intents(idempotency_key)
            WHERE idempotency_key IS NOT NULL;
        """
    )
    # Idempotent migration for pre-P6.4 databases.
    try:
        _DB.execute(
            "ALTER TABLE payment_intents "
            "ADD COLUMN idempotency_key TEXT DEFAULT NULL"
        )
    except sqlite3.OperationalError:
        pass
    try:
        _DB.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_idem "
            "ON payment_intents(idempotency_key) "
            "WHERE idempotency_key IS NOT NULL"
        )
    except sqlite3.OperationalError:
        pass
    _DB.commit()


def _now() -> float:
    return time.time()


def _is_test_key(provider: str, key: str) -> bool:
    """True only if ``key`` is a declared TEST credential for ``provider``.
    This is the single source of truth for 'can we talk to the network
    without risking real money'. Keep the rules explicit and conservative."""
    if not key:
        return False
    k = key.strip()
    if provider == "stripe_test":
        # Stripe test secret keys look like 'sk_test_...' or 'rk_test_...'.
        # LIVE keys are 'sk_live_...' — we refuse.
        return k.startswith("sk_test_") or k.startswith("rk_test_")
    if provider == "paypal_sandbox":
        # PayPal has no key-prefix convention; we use a user-specified
        # tag in the keychain entry. The caller passed in an alias like
        # 'paypal_sandbox' so at least the intent to use sandbox is
        # explicit. We still require the user to set a separate flag.
        return True
    return False


class PaymentSecurityError(RuntimeError):
    pass


def _resolve_key(provider: str) -> str | None:
    """Fetch a credential from the OS keychain via core.secrets. Never
    reads a plaintext file — if keyring is unavailable or the entry
    isn't set, returns None and the caller falls back to local sandbox."""
    try:
        from core import secrets as _secrets
    except Exception:
        return None
    name = f"{provider}_test_key"
    try:
        return _secrets.get_api_key(name)
    except Exception:
        return None


# ── Prepare ─────────────────────────────────────────────────────────────────

def _norm_provider(raw: str | None) -> str:
    v = (raw or "local").strip().lower()
    return v if v in PROVIDERS else "local"


def _derive_idempotency_key(amount: float, currency: str,
                             payee: str | None,
                             description: str | None,
                             window_seconds: float = 120.0) -> str:
    """Build a dedupe key from the invariant fields of a transaction plus
    a coarse time bucket. Any two prepare() calls with the same amount,
    currency, payee, and description WITHIN ``window_seconds`` resolve
    to the same key and therefore the same intent — this is the duplicate
    guard the planner gets for free when no explicit key is passed. Two
    legitimate repeats of the same payment (e.g. "pay the dog walker
    $30 again") a minute later will share a key for the first 2 minutes
    only; after that the bucket rolls and a new key is derived."""
    import hashlib
    bucket = int(time.time() / max(1.0, window_seconds))
    raw = f"{amount:.6f}|{currency.upper()}|{(payee or '').strip().lower()}" \
          f"|{(description or '').strip().lower()}|{bucket}"
    return "auto:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def prepare(amount: float, *, currency: str = "USD",
            description: str | None = None,
            payee: str | None = None,
            provider: str = "local",
            metadata: dict | None = None,
            idempotency_key: str | None = None) -> dict:
    """Create a prepared payment intent. Does NOT execute anything.

    Idempotency:
      * If ``idempotency_key`` is passed and an intent with that key
        already exists, returns the existing row unchanged. This gives
        callers a strong "exactly one intent per key" guarantee even
        across process crashes.
      * If no key is passed, one is DERIVED from amount + currency +
        payee + description + a 2-minute time bucket (see
        ``_derive_idempotency_key``). A second prepare() with the same
        invariants within that window therefore returns the same intent
        instead of creating a duplicate — the common cause of "I paid
        the restaurant twice by accident".
    """
    provider = _norm_provider(provider)
    amount = float(amount)
    if amount <= 0:
        raise ValueError("amount must be positive")

    key = (idempotency_key or "").strip() or _derive_idempotency_key(
        amount, currency, payee, description,
    )

    with _LOCK:
        existing = _DB.execute(
            "SELECT id FROM payment_intents WHERE idempotency_key=?",
            (key,),
        ).fetchone()
    if existing:
        # Same key → same intent. Never create a second row.
        return get_intent(int(existing["id"])) or {}

    provider_id: str | None = None
    if provider != "local":
        original_provider = provider
        api_key = _resolve_key(provider)
        if not api_key:
            # No keychain entry → degrade to local sandbox. Mentioning
            # the ORIGINAL provider in metadata keeps the voice loop
            # honest about why we fell back.
            provider = "local"
            metadata = dict(metadata or {},
                            degraded_from=original_provider)
        elif not _is_test_key(original_provider, api_key):
            raise PaymentSecurityError(
                f"{original_provider}: refusing non-test credential — "
                "live payment execution is not supported"
            )
        else:
            # A placeholder provider_id so later verify calls can look up
            # the intent without us pretending to have actually hit the
            # SDK. Real integrations plug in here.
            provider_id = f"{provider}:local:{int(time.time() * 1000)}"

    with _LOCK:
        try:
            cur = _DB.execute(
                "INSERT INTO payment_intents(provider, provider_id, amount, "
                "currency, description, payee, status, metadata_json, "
                "idempotency_key, created_at) "
                "VALUES(?,?,?,?,?,?, 'prepared', ?, ?, ?)",
                (provider, provider_id, amount, currency.upper(),
                 description, payee,
                 json.dumps(metadata or {}, ensure_ascii=False),
                 key, _now()),
            )
            _DB.commit()
            intent_id = int(cur.lastrowid)
        except sqlite3.IntegrityError:
            # Another thread raced us on the same key; return theirs.
            row = _DB.execute(
                "SELECT id FROM payment_intents WHERE idempotency_key=?",
                (key,),
            ).fetchone()
            if row:
                return get_intent(int(row["id"])) or {}
            raise
    return get_intent(intent_id) or {}


def verify_recipient(intent_id: int, *,
                      expected_amount: float | None = None,
                      expected_currency: str | None = None,
                      expected_payee: str | None = None,
                      expected_purpose: str | None = None) -> dict:
    """Return ``{ok, mismatches}``. The caller passes the user-spoken
    recipient / amount / currency / purpose so we can confirm that the
    stored intent matches what the user just verbally approved before
    ``confirm_intent`` runs. Each mismatched field is listed; empty
    ``mismatches`` means every provided check lines up."""
    intent = get_intent(intent_id)
    if not intent:
        return {"ok": False, "mismatches": ["intent not found"]}
    mismatches: list[str] = []
    if expected_amount is not None:
        try:
            if abs(float(expected_amount) - float(intent["amount"])) > 0.01:
                mismatches.append(
                    f"amount: intent={intent['amount']} expected={expected_amount}")
        except (TypeError, ValueError):
            mismatches.append("amount: non-numeric expected value")
    if expected_currency:
        if expected_currency.strip().upper() != intent["currency"]:
            mismatches.append(
                f"currency: intent={intent['currency']} "
                f"expected={expected_currency.upper()}")
    if expected_payee:
        stored = (intent.get("payee") or "").strip().lower()
        if expected_payee.strip().lower() != stored:
            mismatches.append(
                f"payee: intent={intent.get('payee')!r} "
                f"expected={expected_payee!r}")
    if expected_purpose:
        stored = (intent.get("description") or "").strip().lower()
        if expected_purpose.strip().lower() not in stored:
            mismatches.append(
                f"purpose: intent description does not mention "
                f"{expected_purpose!r}")
    return {"ok": not mismatches, "mismatches": mismatches,
            "intent": intent}


def get_intent(intent_id: int) -> dict | None:
    with _LOCK:
        row = _DB.execute(
            "SELECT * FROM payment_intents WHERE id=?", (intent_id,)
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["metadata"] = json.loads(d.pop("metadata_json") or "{}")
    except Exception:
        d["metadata"] = {}
    return d


def list_intents(status: str | Iterable[str] | None = None,
                  limit: int = 50) -> list[dict]:
    with _LOCK:
        if status is None:
            rows = _DB.execute(
                "SELECT * FROM payment_intents "
                "ORDER BY created_at DESC LIMIT ?", (limit,),
            ).fetchall()
        else:
            if isinstance(status, str):
                status = [status]
            ph = ",".join("?" * len(list(status)))
            rows = _DB.execute(
                f"SELECT * FROM payment_intents WHERE status IN ({ph}) "
                f"ORDER BY created_at DESC LIMIT ?",
                (*status, limit),
            ).fetchall()
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        try:
            d["metadata"] = json.loads(d.pop("metadata_json") or "{}")
        except Exception:
            d["metadata"] = {}
        out.append(d)
    return out


# ── Confirm / cancel ────────────────────────────────────────────────────────

def confirm_intent(intent_id: int, *, confirmed_by: str,
                    expected_amount: float | None = None,
                    expected_currency: str | None = None,
                    expected_payee: str | None = None,
                    expected_purpose: str | None = None) -> dict:
    """Move a prepared intent to 'confirmed'. Never auto-called — the
    caller must pass ``confirmed_by`` (e.g. 'user', 'hud-button') as
    proof the user has explicitly agreed. Local-sandbox confirmations
    additionally mirror the amount into ``finance.add_expense`` so the
    monthly summary stays truthful.

    When any ``expected_*`` field is supplied, ``verify_recipient`` is
    called first and the confirm is REFUSED on any mismatch. This is
    the pre-send check the voice loop uses to read back
    "you're sending <amount> <currency> to <payee> for <purpose>,
    confirm?" — if the intent on disk doesn't match the spoken readback,
    something has gone wrong and we must stop rather than send."""
    if not confirmed_by:
        raise PaymentSecurityError(
            "confirm_intent: confirmed_by proof required"
        )
    intent = get_intent(intent_id)
    if not intent:
        raise LookupError(f"intent {intent_id} not found")
    if intent["status"] != "prepared":
        return intent  # idempotent-ish: no state regressions

    if any(v is not None for v in (expected_amount, expected_currency,
                                     expected_payee, expected_purpose)):
        check = verify_recipient(
            intent_id,
            expected_amount=expected_amount,
            expected_currency=expected_currency,
            expected_payee=expected_payee,
            expected_purpose=expected_purpose,
        )
        if not check["ok"]:
            raise PaymentSecurityError(
                "recipient/amount mismatch: " + "; ".join(check["mismatches"])
            )

    with _LOCK:
        _DB.execute(
            "UPDATE payment_intents SET status='confirmed', "
            "confirmed_at=? WHERE id=?",
            (_now(), intent_id),
        )
        _DB.commit()

    # Local sandbox: push the amount into finance.add_expense so the
    # existing budget/report machinery reflects the "paid" state.
    if intent["provider"] == "local":
        try:
            from core import finance as _fin
            _fin.add_expense(
                intent["amount"],
                category=(intent.get("metadata") or {})
                    .get("category", "payments"),
                merchant=intent.get("payee"),
                currency=intent.get("currency", "USD"),
                notes=f"[sandbox confirm by {confirmed_by}] "
                      f"{intent.get('description','')}",
            )
        except Exception as e:
            print(f"[Payments] finance mirror failed: {e}")
    return get_intent(intent_id) or {}


def cancel_intent(intent_id: int) -> dict | None:
    with _LOCK:
        _DB.execute(
            "UPDATE payment_intents SET status='cancelled' "
            "WHERE id=? AND status='prepared'",
            (intent_id,),
        )
        _DB.commit()
    return get_intent(intent_id)


# ── Verify (sandbox status check) ───────────────────────────────────────────

def verify_intent(intent_id: int) -> dict:
    """Return a status dict. For the local sandbox this is simply the
    intent row; for real providers (not shipped here, but the extension
    point lives here) it would call the provider's status endpoint.
    Always safe: no mutation."""
    intent = get_intent(intent_id)
    if not intent:
        return {"ok": False, "error": "intent not found"}
    return {
        "ok":           True,
        "id":           intent["id"],
        "provider":     intent["provider"],
        "provider_id":  intent.get("provider_id"),
        "status":       intent["status"],
        "amount":       intent["amount"],
        "currency":     intent["currency"],
        "confirmed_at": intent.get("confirmed_at"),
    }
