"""
core/webhooks.py — inbound webhook registry (P4.9).

A thin registry mapping webhook name → HMAC secret + handler callable.
Separated from `dashboard/server.py` so unit tests do not need to spin up
the full FastAPI app, and so non-dashboard entry points (future CLI,
test rig, replay tool) can share the same verification logic.

Signature scheme:

    X-JARVIS-Signature: sha256=<hex>

where `<hex>` is `HMAC-SHA256(secret, raw_request_body)`. Rejected on
mismatch or absent header. `hmac.compare_digest` guards against timing
oracles.
"""
from __future__ import annotations

import hmac
import threading
from hashlib import sha256
from typing import Any, Callable

from core.logging_setup import get_logger

_log = get_logger(__name__)

_HANDLER = Callable[[bytes, dict], Any]


class WebhookRegistry:
    """Thread-safe name → (secret, handler) map."""

    def __init__(self):
        self._lock = threading.RLock()
        self._entries: dict[str, tuple[bytes, _HANDLER]] = {}

    def register(self, name: str, secret: str, handler: _HANDLER) -> None:
        if not name or not isinstance(name, str):
            raise ValueError("name is required")
        if not secret:
            raise ValueError("secret is required")
        with self._lock:
            self._entries[name] = (secret.encode("utf-8"), handler)

    def unregister(self, name: str) -> bool:
        with self._lock:
            return self._entries.pop(name, None) is not None

    def list_names(self) -> list[str]:
        with self._lock:
            return sorted(self._entries.keys())

    def verify(self, name: str, body: bytes, signature: str | None) -> bool:
        """Return True iff `signature` matches HMAC(secret, body) for `name`.
        Unknown webhooks + missing / malformed signatures return False
        without leaking whether the name existed."""
        with self._lock:
            entry = self._entries.get(name)
        if entry is None or not signature:
            return False
        secret, _ = entry
        expected = _compute_signature(secret, body)
        candidate = signature.strip()
        if candidate.lower().startswith("sha256="):
            candidate = candidate.split("=", 1)[1].strip()
        try:
            return hmac.compare_digest(expected.lower(), candidate.lower())
        except Exception:
            return False

    def dispatch(self, name: str, body: bytes, headers: dict) -> Any:
        """Call the registered handler. Caller must `verify()` first; this
        method does NOT re-check the signature."""
        with self._lock:
            entry = self._entries.get(name)
        if entry is None:
            raise KeyError(name)
        _, handler = entry
        return handler(body, headers)


_REGISTRY = WebhookRegistry()


def default_registry() -> WebhookRegistry:
    """Process-wide singleton, used by `dashboard/server.py`."""
    return _REGISTRY


def _compute_signature(secret: bytes, body: bytes) -> str:
    return hmac.new(secret, body, sha256).hexdigest()
