"""Worker enqueues to NotifyQueue on terminal task transitions."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from core import journal, tasks
from core.notify import NotifyQueue
from core.worker import TaskWorker


def _tmp(prefix: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix))


class WorkerNotifyHookupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tasks._SUBSCRIBERS.clear()
        base = _tmp("worker-notify-")
        tasks.connect(base / "tasks.sqlite")
        journal.connect(base / "journal")
        self.queue = NotifyQueue(base / "notify.sqlite")

    def tearDown(self):
        tasks._SUBSCRIBERS.clear()

    async def _drive(self, w, tid, max_ticks=20):
        run = asyncio.create_task(w.run())
        for _ in range(max_ticks):
            await asyncio.sleep(0.05)
            if tasks.get_task(tid)["status"] in ("done", "failed", "cancelled"):
                break
        w.stop()
        try:
            await asyncio.wait_for(run, timeout=2)
        except Exception:
            pass

    async def test_done_task_enqueues_one_row(self):
        async def dispatch(tool, args):
            return "all good"

        w = TaskWorker(dispatch=dispatch, notify=lambda _m: None,
                       notify_queue=self.queue)
        tid = tasks.create_task("ship it")
        tasks.add_step(tid, 1, "finish", "echo", {}, [],
                       verify={"kind": "result_contains", "text": "good"})
        await self._drive(w, tid)
        self.assertEqual(tasks.get_task(tid)["status"], "done")
        self.assertEqual(self.queue.pending_count(), 1)

    async def test_failed_task_enqueues_one_row(self):
        async def dispatch(tool, args):
            raise RuntimeError("down")

        orig_repair = TaskWorker._repair
        async def no_repair(self, *a, **kw): return None
        TaskWorker._repair = no_repair
        try:
            w = TaskWorker(dispatch=dispatch, notify=lambda _m: None,
                           notify_queue=self.queue)
            tid = tasks.create_task("doomed")
            tasks.add_step(tid, 1, "x", "boom", {}, [])
            await self._drive(w, tid)
            self.assertEqual(tasks.get_task(tid)["status"], "failed")
            self.assertEqual(self.queue.pending_count(), 1)
        finally:
            TaskWorker._repair = orig_repair

    async def test_dedup_prevents_double_enqueue(self):
        """If _maybe_finalise_task runs twice for the same terminal task,
        the dedup key stops the second enqueue."""
        async def dispatch(tool, args):
            return "ok"

        w = TaskWorker(dispatch=dispatch, notify=lambda _m: None,
                       notify_queue=self.queue)
        tid = tasks.create_task("once")
        tasks.add_step(tid, 1, "v", "echo", {}, [],
                       verify={"kind": "result_not_empty"})
        await self._drive(w, tid)
        # Call again manually — dedup should hold.
        w._maybe_finalise_task(tid)
        self.assertEqual(self.queue.pending_count(), 1)

    async def test_no_queue_wired_is_fine(self):
        """Existing TaskWorker callers pass no queue — must still work."""
        async def dispatch(tool, args):
            return "ok"

        w = TaskWorker(dispatch=dispatch, notify=lambda _m: None)
        tid = tasks.create_task("legacy")
        tasks.add_step(tid, 1, "v", "echo", {}, [],
                       verify={"kind": "result_not_empty"})
        await self._drive(w, tid)
        self.assertEqual(tasks.get_task(tid)["status"], "done")


if __name__ == "__main__":
    unittest.main()
