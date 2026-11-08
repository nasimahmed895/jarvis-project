"""P1.5 — event-driven worker wake-up."""

from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path

from core import journal, tasks
from core.worker import TaskWorker


def _tmp(prefix: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix))


class SubscribeUnitTests(unittest.TestCase):
    """Raw mutation points fire subscribers."""

    def setUp(self):
        tasks._SUBSCRIBERS.clear()
        base = _tmp("tasks-events-")
        tasks.connect(base / "tasks.sqlite")

    def tearDown(self):
        tasks._SUBSCRIBERS.clear()

    def test_create_task_fires_subscriber(self):
        hits = []
        tasks.subscribe(lambda: hits.append("t"))
        tasks.create_task("g")
        self.assertEqual(len(hits), 1)

    def test_add_step_and_status_fire(self):
        hits = []
        tasks.subscribe(lambda: hits.append("x"))
        tid = tasks.create_task("g")           # 1
        sid = tasks.add_step(tid, 1, "v", "speak", {}, [])   # 2
        tasks.set_step_status(sid, "running")  # 3
        tasks.set_step_status(sid, "done", result="ok")      # 4
        tasks.set_task_status(tid, "done")     # 5
        self.assertEqual(len(hits), 5)

    def test_cancel_task_fires(self):
        hits = []
        tid = tasks.create_task("g")
        tasks.subscribe(lambda: hits.append("c"))
        tasks.cancel_task(tid)
        self.assertEqual(len(hits), 1)

    def test_unsubscribe(self):
        hits = []
        cb = lambda: hits.append("x")
        tasks.subscribe(cb)
        tasks.unsubscribe(cb)
        tasks.create_task("g")
        self.assertEqual(hits, [])

    def test_subscriber_exception_does_not_break_mutation(self):
        def boom(): raise RuntimeError("bad")
        tasks.subscribe(boom)
        # Should not raise.
        tid = tasks.create_task("g")
        self.assertGreater(tid, 0)


class WorkerLatencyTests(unittest.IsolatedAsyncioTestCase):
    """Create a task while worker is idle; dispatch must land fast."""

    def setUp(self):
        tasks._SUBSCRIBERS.clear()
        base = _tmp("worker-events-")
        tasks.connect(base / "tasks.sqlite")
        journal.connect(base / "journal")

    def tearDown(self):
        tasks._SUBSCRIBERS.clear()

    async def test_create_task_wakes_worker_fast(self):
        dispatched = asyncio.Event()

        async def dispatch(tool, args):
            dispatched.set()
            return "ok"

        w = TaskWorker(dispatch=dispatch, notify=lambda _m: None)
        run_task = asyncio.create_task(w.run())

        # Give the worker a moment to reach its wait().
        await asyncio.sleep(0.05)

        start = time.monotonic()
        tid = tasks.create_task("wake me")
        tasks.add_step(tid, 1, "v", "echo", {}, [],
                       verify={"kind": "result_not_empty"})

        try:
            await asyncio.wait_for(dispatched.wait(), timeout=1.0)
        finally:
            w.stop()
            try:
                await asyncio.wait_for(run_task, timeout=2.0)
            except Exception:
                pass

        elapsed = time.monotonic() - start
        # Old poll-only path would need up to 500ms; event path should be
        # well under 200ms even on a slow CI runner.
        self.assertLess(elapsed, 0.3,
                        f"dispatch took {elapsed*1000:.0f}ms — not event-driven")


if __name__ == "__main__":
    unittest.main()
