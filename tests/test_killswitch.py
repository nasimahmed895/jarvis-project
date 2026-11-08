"""P2.6 — emergency stop (killswitch)."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import journal, tasks
from core import killswitch
from core.worker import TaskWorker


def _tmp(prefix: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix))


class KillswitchUnitTests(unittest.TestCase):
    def setUp(self):
        killswitch.reset()
        killswitch.clear_listeners()
        self._flag_dir = _tmp("ks-")
        self._flag_path = self._flag_dir / ".jarvis_killswitch"
        self._patch = mock.patch.object(killswitch, "_FLAG_PATH", self._flag_path)
        self._patch.start()
        journal.connect(self._flag_dir / "journal")

    def tearDown(self):
        killswitch.reset()
        killswitch.clear_listeners()
        self._patch.stop()

    def test_trigger_latches_state(self):
        self.assertFalse(killswitch.is_active())
        killswitch.trigger("unit-test")
        self.assertTrue(killswitch.is_active())
        self.assertEqual(killswitch.status()["reason"], "unit-test")

    def test_trigger_is_idempotent(self):
        hits: list[str] = []
        killswitch.on_trigger(hits.append)
        killswitch.trigger("one")
        killswitch.trigger("two")   # ignored while latched
        self.assertEqual(hits, ["one"])

    def test_flag_file_activates(self):
        self._flag_path.write_text("stop", encoding="utf-8")
        self.assertTrue(killswitch.is_active())
        self.assertEqual(killswitch.status()["reason"], "flag-file")

    def test_reset_clears_flag_file(self):
        self._flag_path.write_text("stop", encoding="utf-8")
        self.assertTrue(killswitch.is_active())
        killswitch.reset()
        self.assertFalse(killswitch.is_active())
        self.assertFalse(self._flag_path.exists())

    def test_listener_exception_is_contained(self):
        def boom(_r): raise RuntimeError("bad listener")
        killswitch.on_trigger(boom)
        # Must not raise.
        killswitch.trigger("test")
        self.assertTrue(killswitch.is_active())


class KillswitchWorkerIntegration(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tasks._SUBSCRIBERS.clear()
        killswitch.reset()
        killswitch.clear_listeners()
        base = _tmp("ks-worker-")
        tasks.connect(base / "tasks.sqlite")
        journal.connect(base / "journal")
        flag = base / ".jarvis_killswitch"
        self._patch = mock.patch.object(killswitch, "_FLAG_PATH", flag)
        self._patch.start()
        self._flag = flag

    def tearDown(self):
        killswitch.reset()
        killswitch.clear_listeners()
        tasks._SUBSCRIBERS.clear()
        self._patch.stop()

    async def test_worker_stops_when_killswitch_arms(self):
        async def dispatch(tool, args):
            await asyncio.sleep(0.05)
            return "ok"

        w = TaskWorker(dispatch=dispatch, notify=lambda _m: None)
        tid = tasks.create_task("doomed")
        tasks.add_step(tid, 1, "v", "echo", {}, [])
        run_task = asyncio.create_task(w.run())
        await asyncio.sleep(0.05)
        killswitch.trigger("test-stop")
        try:
            await asyncio.wait_for(run_task, timeout=2.0)
        except asyncio.TimeoutError:
            self.fail("worker did not stop under killswitch within 2s")


if __name__ == "__main__":
    unittest.main()
