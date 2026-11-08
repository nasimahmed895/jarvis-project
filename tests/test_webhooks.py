"""P4.9 — inbound webhook registry + dashboard route."""

from __future__ import annotations

import hashlib
import hmac
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from core.webhooks import WebhookRegistry, default_registry, _compute_signature
from dashboard import server as dash


def _tmp(prefix: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix))


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + _compute_signature(secret.encode("utf-8"), body)


# ── Registry ─────────────────────────────────────────────────────────────────

class WebhookRegistryTests(unittest.TestCase):
    def setUp(self):
        self.r = WebhookRegistry()
        self.r.register("gh", "secret-1", lambda b, h: {"len": len(b)})

    def test_verify_accepts_valid_signature(self):
        body = b"{}"
        self.assertTrue(self.r.verify("gh", body, _sign("secret-1", body)))

    def test_verify_rejects_tampered_body(self):
        body = b"{}"
        sig = _sign("secret-1", body)
        self.assertFalse(self.r.verify("gh", b"{tampered}", sig))

    def test_verify_rejects_wrong_secret(self):
        body = b"{}"
        self.assertFalse(self.r.verify("gh", body, _sign("other", body)))

    def test_verify_rejects_unknown_name_quietly(self):
        self.assertFalse(self.r.verify("missing", b"", _sign("x", b"")))

    def test_verify_missing_signature(self):
        self.assertFalse(self.r.verify("gh", b"{}", None))

    def test_dispatch_invokes_handler(self):
        out = self.r.dispatch("gh", b"hello", {"H": "v"})
        self.assertEqual(out, {"len": 5})

    def test_dispatch_unknown_raises(self):
        with self.assertRaises(KeyError):
            self.r.dispatch("missing", b"", {})

    def test_list_names_sorted(self):
        self.r.register("alpha", "s", lambda b, h: None)
        self.assertEqual(self.r.list_names(), ["alpha", "gh"])

    def test_unregister(self):
        self.assertTrue(self.r.unregister("gh"))
        self.assertFalse(self.r.unregister("gh"))


# ── Dashboard route ──────────────────────────────────────────────────────────

class WebhookRouteTests(unittest.TestCase):
    def setUp(self):
        tmp_base = _tmp("wh-")
        (tmp_base / "memory").mkdir(parents=True, exist_ok=True)
        with mock.patch.object(dash, "BASE_DIR", tmp_base):
            self.server = dash.DashboardServer()
        self.client = TestClient(self.server.app)
        # Clear any leftovers from other tests on the shared singleton.
        reg = default_registry()
        for n in reg.list_names():
            reg.unregister(n)

    def tearDown(self):
        reg = default_registry()
        for n in reg.list_names():
            reg.unregister(n)

    def test_valid_webhook_dispatches(self):
        received: list[bytes] = []
        default_registry().register(
            "svc", "topsecret",
            lambda body, headers: received.append(body) or {"ack": True},
        )
        body = b'{"event":"ping"}'
        r = self.client.post(
            "/webhook/svc",
            content=body,
            headers={"X-JARVIS-Signature": _sign("topsecret", body)},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(received, [body])
        self.assertEqual(r.json()["result"], {"ack": True})

    def test_rejects_bad_signature(self):
        default_registry().register("svc", "topsecret", lambda b, h: None)
        r = self.client.post(
            "/webhook/svc", content=b"{}",
            headers={"X-JARVIS-Signature": "sha256=deadbeef"},
        )
        self.assertEqual(r.status_code, 401)

    def test_rejects_missing_signature(self):
        default_registry().register("svc", "topsecret", lambda b, h: None)
        r = self.client.post("/webhook/svc", content=b"{}")
        self.assertEqual(r.status_code, 401)

    def test_unknown_webhook_returns_401(self):
        # Verification fails before dispatch, so unknown name gets the same
        # 401 as a bad signature — this avoids an enumeration oracle.
        r = self.client.post(
            "/webhook/missing", content=b"{}",
            headers={"X-JARVIS-Signature": "sha256=aa"},
        )
        self.assertEqual(r.status_code, 401)

    def test_handler_exception_bubbles_500(self):
        def boom(_b, _h): raise RuntimeError("down")
        default_registry().register("svc", "s", boom)
        body = b"{}"
        r = self.client.post(
            "/webhook/svc", content=body,
            headers={"X-JARVIS-Signature": _sign("s", body)},
        )
        self.assertEqual(r.status_code, 500)


if __name__ == "__main__":
    unittest.main()
