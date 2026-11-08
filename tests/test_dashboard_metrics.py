"""P3.4 — /api/v1/metrics system health endpoint."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from dashboard import server as dash


def _tmp(prefix: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix))


def _auth_client():
    tmp_base = _tmp("metrics-")
    (tmp_base / "memory").mkdir(parents=True, exist_ok=True)
    with mock.patch.object(dash, "BASE_DIR", tmp_base):
        server = dash.DashboardServer()
    token = "t"
    server._tokens.add(token)
    return TestClient(server.app), {"Authorization": f"Bearer {token}"}


class MetricsEndpointTests(unittest.TestCase):
    def test_requires_auth(self):
        client, _ = _auth_client()
        r = client.get("/api/v1/metrics")
        self.assertEqual(r.status_code, 401)

    def test_returns_known_shape(self):
        client, headers = _auth_client()
        r = client.get("/api/v1/metrics", headers=headers)
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIn("uptime_s", data)
        # psutil probably present; if import fails we still get the uptime +
        # an error string rather than a crash.
        self.assertTrue("cpu_percent" in data or "error" in data)

    def test_ram_and_disk_fields_when_psutil_available(self):
        client, headers = _auth_client()
        r = client.get("/api/v1/metrics", headers=headers)
        data = r.json()
        if "cpu_percent" in data:
            self.assertIn("percent", data["ram"])
            self.assertIn("percent", data["disk"])
            self.assertIn("process_count", data)


if __name__ == "__main__":
    unittest.main()
