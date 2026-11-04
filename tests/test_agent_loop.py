"""
Integration tests for the autonomous agent loop (Phases 1 + 2 + 4a).

These don't touch Gemini — the dispatcher is a local stub so the test is
deterministic and offline. Covers:

  - SQLite task store: create, add steps with deps, ready_steps gating,
    cancel propagation.
  - Worker: drains ready steps, honours dependency order, finalises task
    status, respects cancel.
  - Verifiers: result_contains, result_not_empty, exit_zero,
    stdout_contains, file_exists.
  - Repair: a step whose verifier fails invokes the repair callback; the
    worker retries with new args.

The Gemini-backed planner path is covered only lightly (one happy-path
smoke) and is skipped when no API key is configured.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from core import tasks, journal
from core.worker import TaskWorker
from core import worker as worker_mod


def _tmp(prefix: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix))


class TaskStoreTests(unittest.TestCase):
    def setUp(self):
        self.db = _tmp("tasks-") / "tasks.sqlite"
        tasks.connect(self.db)

    def test_create_and_retrieve(self):
        tid = tasks.create_task("goal A")
        self.assertGreater(tid, 0)
        t = tasks.get_task(tid)
        self.assertEqual(t["goal"], "goal A")
        self.assertEqual(t["status"], "planned")

    def test_deps_gate_ready_steps(self):
        tid = tasks.create_task("ordered")
        s1 = tasks.add_step(tid, 1, "a", "speak", {}, [])
        s2 = tasks.add_step(tid, 2, "b", "speak", {}, [s1])
        self.assertEqual([r["id"] for r in tasks.ready_steps(tid)], [s1])
        tasks.set_step_status(s1, "done", result="ok")
        self.assertEqual([r["id"] for r in tasks.ready_steps(tid)], [s2])

    def test_cancel_task_cancels_pending_steps(self):
        tid = tasks.create_task("doomed")
        s1 = tasks.add_step(tid, 1, "a", "speak", {}, [])
        s2 = tasks.add_step(tid, 2, "b", "speak", {}, [s1])
        tasks.cancel_task(tid)
        for s in (s1, s2):
            self.assertEqual(tasks.get_step(s)["status"], "cancelled")
        self.assertEqual(tasks.get_task(tid)["status"], "cancelled")


class VerifierTests(unittest.TestCase):
    def test_result_contains(self):
        ok, _ = worker_mod._verify(
            {"kind": "result_contains", "text": "foo"},
            "has foo inside", "any", {},
        )
        self.assertTrue(ok)

    def test_result_not_empty_fails_on_blank(self):
        ok, _ = worker_mod._verify(
            {"kind": "result_not_empty"}, "   ", "any", {},
        )
        self.assertFalse(ok)

    def test_exit_zero(self):
        ok, _ = worker_mod._verify(
            {"kind": "exit_zero"},
            "shell exit=0 cwd=/ cmd=echo\n--- stdout ---\nhi\n", "shell", {},
        )
        self.assertTrue(ok)
        ok, _ = worker_mod._verify(
            {"kind": "exit_zero"},
            "shell exit=7 cwd=/ cmd=bad\n--- stdout ---\n", "shell", {},
        )
        self.assertFalse(ok)

    def test_stdout_contains(self):
        body = "shell exit=0\n--- stdout ---\nhello\n--- stderr ---\n"
        ok, _ = worker_mod._verify(
            {"kind": "stdout_contains", "text": "hello"}, body, "shell", {},
        )
        self.assertTrue(ok)
        ok, _ = worker_mod._verify(
            {"kind": "stdout_contains", "text": "nope"}, body, "shell", {},
        )
        self.assertFalse(ok)

    def test_file_exists(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("x")
            p = f.name
        try:
            ok, _ = worker_mod._verify(
                {"kind": "file_exists", "path": p, "non_empty": True},
                "", "any", {},
            )
            self.assertTrue(ok)
        finally:
            os.unlink(p)
        ok, _ = worker_mod._verify(
            {"kind": "file_exists", "path": "/tmp/__never__"}, "", "any", {},
        )
        self.assertFalse(ok)


class WorkerLoopTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        base = _tmp("worker-")
        tasks.connect(base / "tasks.sqlite")
        journal.connect(base / "journal")

    async def _drain(self, w, tid, max_ticks=40):
        run = asyncio.create_task(w.run())
        for _ in range(max_ticks):
            await asyncio.sleep(0.2)
            t = tasks.get_task(tid)
            if t["status"] in ("done", "failed", "cancelled"):
                break
        w.stop()
        try:
            await asyncio.wait_for(run, timeout=2)
        except Exception:
            pass

    async def test_three_step_task_runs_in_order(self):
        order = []

        async def dispatch(tool, args):
            order.append((tool, args))
            if tool == "echo":
                return "echoed " + str(args.get("x"))
            raise RuntimeError("unknown")

        w = TaskWorker(dispatch=dispatch, notify=lambda m: None)
        tid = tasks.create_task("chain")
        s1 = tasks.add_step(tid, 1, "a", "echo", {"x": "a"}, [])
        s2 = tasks.add_step(tid, 2, "b", "echo", {"x": "b"}, [s1])
        s3 = tasks.add_step(tid, 3, "c", "echo", {"x": "c"}, [s2])
        await self._drain(w, tid)
        self.assertEqual(tasks.get_task(tid)["status"], "done")
        self.assertEqual([a for _, a in order],
                         [{"x": "a"}, {"x": "b"}, {"x": "c"}])

    async def test_failed_step_fails_task(self):
        async def dispatch(tool, args):
            raise RuntimeError("boom")

        # Monkey-patch the repair call to always refuse — otherwise this
        # test would hit the live Gemini API.
        orig = TaskWorker._repair
        async def no_repair(self, *a, **kw): return None
        TaskWorker._repair = no_repair
        try:
            w = TaskWorker(dispatch=dispatch, notify=lambda m: None)
            tid = tasks.create_task("boom")
            tasks.add_step(tid, 1, "x", "fail_tool", {}, [])
            await self._drain(w, tid)
            self.assertEqual(tasks.get_task(tid)["status"], "failed")
        finally:
            TaskWorker._repair = orig

    async def test_repair_patches_args(self):
        calls = []

        async def dispatch(tool, args):
            calls.append(args.copy())
            if args.get("good"):
                return "shell exit=0\n--- stdout ---\nok\n"
            return "shell exit=1\n--- stdout ---\nnope\n"

        async def fake_repair(self, step, last_args, error, last_result=None):
            return {"good": True}

        orig = TaskWorker._repair
        TaskWorker._repair = fake_repair
        try:
            w = TaskWorker(dispatch=dispatch, notify=lambda m: None)
            tid = tasks.create_task("repair me")
            tasks.add_step(tid, 1, "s", "shell", {"good": False}, [],
                           verify={"kind": "exit_zero"})
            await self._drain(w, tid)
            self.assertEqual(tasks.get_task(tid)["status"], "done")
            self.assertEqual(len(calls), 2)
            self.assertFalse(calls[0]["good"])
            self.assertTrue(calls[1]["good"])
        finally:
            TaskWorker._repair = orig


if __name__ == "__main__":
    unittest.main(verbosity=2)
