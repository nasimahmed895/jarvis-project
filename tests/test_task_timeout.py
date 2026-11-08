"""P1.3 — step timeout + duration tracking."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from core import journal, tasks
from core.worker import TaskWorker


def _tmp(prefix: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix))


class StepTimeoutSchemaTests(unittest.TestCase):
    """New columns timeout_s + duration_s round-trip through the store."""

    def setUp(self):
        base = _tmp("tasks-timeout-")
        tasks.connect(base / "tasks.sqlite")

    def test_add_step_persists_timeout_s(self):
        tid = tasks.create_task("g")
        sid = tasks.add_step(tid, 1, "v", "speak", {"text": "hi"}, [],
                             timeout_s=5)
        self.assertEqual(tasks.get_step(sid)["timeout_s"], 5)

    def test_timeout_s_defaults_to_none(self):
        tid = tasks.create_task("g")
        sid = tasks.add_step(tid, 1, "v", "speak", {"text": "hi"}, [])
        self.assertIsNone(tasks.get_step(sid)["timeout_s"])

    def test_duration_s_populated_on_done(self):
        tid = tasks.create_task("g")
        sid = tasks.add_step(tid, 1, "v", "speak", {}, [])
        tasks.set_step_status(sid, "running")
        tasks.set_step_status(sid, "done", result="ok")
        dur = tasks.get_step(sid)["duration_s"]
        self.assertIsNotNone(dur)
        self.assertGreaterEqual(dur, 0.0)

    def test_duration_s_populated_on_failed(self):
        tid = tasks.create_task("g")
        sid = tasks.add_step(tid, 1, "v", "speak", {}, [])
        tasks.set_step_status(sid, "running")
        tasks.set_step_status(sid, "failed", error="x")
        self.assertIsNotNone(tasks.get_step(sid)["duration_s"])

    def test_duration_s_none_when_never_running(self):
        tid = tasks.create_task("g")
        sid = tasks.add_step(tid, 1, "v", "speak", {}, [])
        tasks.cancel_task(tid)
        # cancel_task jumps straight to cancelled without a running transition.
        self.assertIsNone(tasks.get_step(sid)["duration_s"])


class StepTimeoutWorkerTests(unittest.IsolatedAsyncioTestCase):
    """Worker enforces timeout_s and records duration on real execution."""

    def setUp(self):
        base = _tmp("worker-timeout-")
        tasks.connect(base / "tasks.sqlite")
        journal.connect(base / "journal")

    async def _drain(self, w, tid, max_ticks=40):
        run = asyncio.create_task(w.run())
        for _ in range(max_ticks):
            await asyncio.sleep(0.1)
            t = tasks.get_task(tid)
            if t["status"] in ("done", "failed", "cancelled"):
                break
        w.stop()
        try:
            await asyncio.wait_for(run, timeout=2)
        except Exception:
            pass

    async def test_slow_tool_fails_cleanly_with_timeout_error(self):
        async def slow_dispatch(tool, args):
            await asyncio.sleep(2.0)
            return "too late"

        orig_repair = TaskWorker._repair
        async def no_repair(self, *a, **kw): return None
        TaskWorker._repair = no_repair
        try:
            w = TaskWorker(dispatch=slow_dispatch, notify=lambda _m: None)
            tid = tasks.create_task("slow")
            sid = tasks.add_step(tid, 1, "v", "echo", {}, [], timeout_s=1)
            await self._drain(w, tid)
            step = tasks.get_step(sid)
            self.assertEqual(step["status"], "failed")
            self.assertIn("timeout", (step["error"] or "").lower())
            self.assertEqual(tasks.get_task(tid)["status"], "failed")
        finally:
            TaskWorker._repair = orig_repair

    async def test_fast_tool_records_duration(self):
        async def fast_dispatch(tool, args):
            return "done"

        w = TaskWorker(dispatch=fast_dispatch, notify=lambda _m: None)
        tid = tasks.create_task("fast")
        sid = tasks.add_step(tid, 1, "v", "echo", {}, [], timeout_s=5,
                             verify={"kind": "result_contains", "text": "done"})
        await self._drain(w, tid)
        step = tasks.get_step(sid)
        self.assertEqual(step["status"], "done")
        self.assertIsNotNone(step["duration_s"])
        self.assertLess(step["duration_s"], 1.0)

    async def test_no_timeout_runs_unbounded(self):
        """When timeout_s is unset the dispatcher is awaited as-is."""
        async def ok_dispatch(tool, args):
            await asyncio.sleep(0.2)
            return "ok"

        w = TaskWorker(dispatch=ok_dispatch, notify=lambda _m: None)
        tid = tasks.create_task("unbounded")
        sid = tasks.add_step(tid, 1, "v", "echo", {}, [],
                             verify={"kind": "result_not_empty"})
        await self._drain(w, tid)
        step = tasks.get_step(sid)
        self.assertEqual(step["status"], "done")


if __name__ == "__main__":
    unittest.main()
