"""P1.4 — resource gate throttles worker dequeue under load."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from core import journal, tasks
from core.resource_gate import OPEN_GATE, ResourceGate
from core.worker import TaskWorker


def _tmp(prefix: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix))


class ResourceGateUnitTests(unittest.TestCase):
    def test_default_env_ceilings(self):
        g = ResourceGate(cpu_fn=lambda: 10.0, ram_fn=lambda: 20.0)
        self.assertEqual(g.cpu_ceiling, 85.0)
        self.assertEqual(g.ram_ceiling, 90.0)
        self.assertTrue(g.allow())

    def test_cpu_over_ceiling_blocks(self):
        g = ResourceGate(cpu_ceiling=50, ram_ceiling=99,
                         cpu_fn=lambda: 90.0, ram_fn=lambda: 10.0)
        self.assertFalse(g.allow())
        self.assertIn("cpu=", g.last_blocked_reason or "")

    def test_ram_over_ceiling_blocks(self):
        g = ResourceGate(cpu_ceiling=99, ram_ceiling=50,
                         cpu_fn=lambda: 10.0, ram_fn=lambda: 95.0)
        self.assertFalse(g.allow())
        self.assertIn("ram=", g.last_blocked_reason or "")

    def test_reopens_when_load_drops(self):
        readings = iter([(99.0, 10.0), (10.0, 10.0)])
        cpu_val = [0.0]
        ram_val = [0.0]

        def cpu_fn():
            return cpu_val[0]

        def ram_fn():
            return ram_val[0]

        g = ResourceGate(cpu_ceiling=50, ram_ceiling=99,
                         cpu_fn=cpu_fn, ram_fn=ram_fn)
        cpu_val[0], ram_val[0] = next(readings)
        self.assertFalse(g.allow())
        cpu_val[0], ram_val[0] = next(readings)
        self.assertTrue(g.allow())
        self.assertIsNone(g.last_blocked_reason)


class WorkerResourceGateIntegration(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        base = _tmp("worker-gate-")
        tasks.connect(base / "tasks.sqlite")
        journal.connect(base / "journal")

    async def _drive(self, w, ticks=3):
        """Run `ticks` manual ticks without starting the full run loop."""
        for _ in range(ticks):
            await w._tick()
            await asyncio.sleep(0)

    async def test_closed_gate_leaves_steps_pending(self):
        async def dispatch(tool, args):
            return "ok"

        gate = ResourceGate(cpu_ceiling=50, ram_ceiling=99,
                            cpu_fn=lambda: 90.0, ram_fn=lambda: 10.0)
        w = TaskWorker(dispatch=dispatch, notify=lambda _m: None,
                       resource_gate=gate)
        tid = tasks.create_task("g")
        sid = tasks.add_step(tid, 1, "v", "echo", {}, [])
        await self._drive(w, ticks=3)
        # Gate was closed throughout — step must still be planned.
        self.assertEqual(tasks.get_step(sid)["status"], "planned")
        self.assertEqual(tasks.get_task(tid)["status"], "planned")

    async def test_open_gate_dispatches(self):
        async def dispatch(tool, args):
            return "ok"

        w = TaskWorker(dispatch=dispatch, notify=lambda _m: None,
                       resource_gate=OPEN_GATE)
        tid = tasks.create_task("g")
        sid = tasks.add_step(tid, 1, "v", "echo", {}, [],
                             verify={"kind": "result_not_empty"})
        # Give the dispatch task a window to resolve.
        await w._tick()
        await asyncio.sleep(0.05)
        for t in list(w._inflight.values()):
            await t
        self.assertEqual(tasks.get_step(sid)["status"], "done")


if __name__ == "__main__":
    unittest.main()
