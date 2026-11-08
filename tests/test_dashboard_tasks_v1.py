"""P3.3 — dashboard task progress / history / detail endpoints."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from core import tasks
from dashboard import server as dash


def _tmp(prefix: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix))


def _auth_client():
    """Instantiate a DashboardServer pointed at a tmp memory dir, then mint
    a session token so protected endpoints pass auth."""
    tasks._SUBSCRIBERS.clear()
    tmp_base = _tmp("dash-v1-")
    (tmp_base / "memory").mkdir(parents=True, exist_ok=True)
    with mock.patch.object(dash, "BASE_DIR", tmp_base):
        server = dash.DashboardServer()
    token = "test-token"
    server._tokens.add(token)
    tasks.connect(tmp_base / "memory" / "tasks.sqlite")
    client = TestClient(server.app)
    headers = {"Authorization": f"Bearer {token}"}
    return client, headers


class SummariserTests(unittest.TestCase):
    def test_summary_shape_and_pct(self):
        tasks._SUBSCRIBERS.clear()
        tasks.connect(_tmp("sum-") / "tasks.sqlite")
        tid = tasks.create_task("ship it")
        s1 = tasks.add_step(tid, 1, "a", "speak", {}, [])
        s2 = tasks.add_step(tid, 2, "b", "speak", {}, [])
        tasks.set_step_status(s1, "running")
        tasks.set_step_status(s1, "done", result="ok")
        row = tasks.get_task(tid)
        sumry = dash._summarise_task(tasks, row)
        self.assertEqual(sumry["done"], 1)
        self.assertEqual(sumry["total"], 2)
        self.assertEqual(sumry["pct"], 50.0)
        self.assertEqual(sumry["next_step"], "b")


class V1EndpointsTests(unittest.TestCase):
    def test_progress_returns_active_tasks_only(self):
        client, headers = _auth_client()
        # one running, one done.
        tid1 = tasks.create_task("live")
        tasks.add_step(tid1, 1, "v", "speak", {}, [])
        tasks.set_task_status(tid1, "running")
        tid2 = tasks.create_task("history")
        tasks.set_task_status(tid2, "done", result="ok")

        r = client.get("/api/v1/tasks/progress", headers=headers)
        self.assertEqual(r.status_code, 200)
        ids = [i["id"] for i in r.json()["items"]]
        self.assertIn(tid1, ids)
        self.assertNotIn(tid2, ids)

    def test_history_returns_terminal_tasks(self):
        client, headers = _auth_client()
        tid = tasks.create_task("done thing")
        tasks.set_task_status(tid, "done", result="ok")
        r = client.get("/api/v1/tasks/history", headers=headers)
        self.assertEqual(r.status_code, 200)
        ids = [i["id"] for i in r.json()["items"]]
        self.assertIn(tid, ids)

    def test_detail_returns_steps(self):
        client, headers = _auth_client()
        tid = tasks.create_task("detail")
        sid = tasks.add_step(tid, 1, "work", "speak", {}, [])
        r = client.get(f"/api/v1/tasks/{tid}", headers=headers)
        self.assertEqual(r.status_code, 200)
        payload = r.json()
        self.assertEqual(payload["task"]["id"], tid)
        self.assertEqual(payload["steps"][0]["id"], sid)

    def test_detail_404(self):
        client, headers = _auth_client()
        tasks.connect(_tmp("dash-404-") / "tasks.sqlite")
        r = client.get("/api/v1/tasks/9999", headers=headers)
        self.assertEqual(r.status_code, 404)

    def test_progress_requires_auth(self):
        client, _ = _auth_client()
        r = client.get("/api/v1/tasks/progress")
        self.assertEqual(r.status_code, 401)


if __name__ == "__main__":
    unittest.main()
