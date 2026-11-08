"""P2.2 — persistent scheduler (cron / interval / one-shot)."""

from __future__ import annotations

import datetime as _dt
import tempfile
import unittest
from pathlib import Path

from core.scheduler import Scheduler, next_cron_after, parse_cron


def _tmp(prefix: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix))


# ── Cron parser ──────────────────────────────────────────────────────────────

class CronParseTests(unittest.TestCase):
    def test_simple_fields(self):
        mins, hours, doms, months, dows = parse_cron("0 9 * * 1")
        self.assertEqual(mins, {0})
        self.assertEqual(hours, {9})
        self.assertEqual(doms, set(range(1, 32)))
        self.assertEqual(months, set(range(1, 13)))
        self.assertEqual(dows, {1})

    def test_step_and_range(self):
        mins, *_ = parse_cron("*/15 * * * *")
        self.assertEqual(mins, {0, 15, 30, 45})
        mins, *_ = parse_cron("10-14 * * * *")
        self.assertEqual(mins, {10, 11, 12, 13, 14})

    def test_rejects_bad_field_count(self):
        with self.assertRaises(ValueError):
            parse_cron("* * * *")

    def test_rejects_out_of_range(self):
        with self.assertRaises(ValueError):
            parse_cron("60 * * * *")

    def test_next_cron_after_daily(self):
        # Target: 09:00 every day.
        base = _dt.datetime(2026, 11, 8, 8, 0)
        nxt = next_cron_after("0 9 * * *", base)
        self.assertEqual(nxt, _dt.datetime(2026, 11, 8, 9, 0))
        # Past 9? → next day.
        base = _dt.datetime(2026, 11, 8, 10, 0)
        nxt = next_cron_after("0 9 * * *", base)
        self.assertEqual(nxt, _dt.datetime(2026, 11, 9, 9, 0))


# ── Scheduler persistence + tick ─────────────────────────────────────────────

class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.created: list[tuple[str, str | None]] = []
        def create_task(goal, key): self.created.append((goal, key)); return len(self.created)
        self.path = _tmp("sched-") / "s.sqlite"
        self.now = [100.0]
        self.sched = Scheduler(
            self.path,
            create_task_fn=create_task,
            clock=lambda: self.now[0],
        )

    def test_add_once_fires_when_due(self):
        self.sched.add_once("one", run_at=200.0, goal="do thing")
        self.now[0] = 199.0
        self.assertEqual(self.sched.tick(), 0)
        self.now[0] = 200.0
        self.assertEqual(self.sched.tick(), 1)
        # A one-shot disables itself after firing.
        self.now[0] = 300.0
        self.assertEqual(self.sched.tick(), 0)
        self.assertEqual(len(self.created), 1)

    def test_add_interval_reschedules(self):
        sid = self.sched.add_interval("every-5s", 5.0, goal="ping")
        self.assertEqual(self.sched.get(sid)["next_run_at"], 105.0)
        self.now[0] = 110.0
        self.assertEqual(self.sched.tick(), 1)
        # next_run_at advances past `now`, not just +interval.
        self.assertEqual(self.sched.get(sid)["next_run_at"], 115.0)

    def test_interval_catches_up_after_sleep(self):
        """If the process slept through several windows, advance to the
        first future slot — do not fire once per missed slot."""
        self.sched.add_interval("every-5s", 5.0, goal="ping", now=100.0)
        self.now[0] = 200.0
        self.assertEqual(self.sched.tick(), 1)
        self.assertEqual(len(self.created), 1)

    def test_cron_schedule_persists_and_fires(self):
        # 0 9 * * * — every day at 09:00. Seed clock at 08:00 local.
        base = _dt.datetime(2026, 11, 8, 8, 0)
        self.now[0] = base.timestamp()
        self.sched.add_cron("daily-brief", "0 9 * * *", goal="brief")
        row = self.sched.list()[0]
        self.assertEqual(row["kind"], "cron")
        # Advance clock past the firing minute.
        self.now[0] = _dt.datetime(2026, 11, 8, 9, 1).timestamp()
        self.assertEqual(self.sched.tick(), 1)
        # Next fire is tomorrow 09:00.
        nxt = _dt.datetime.fromtimestamp(self.sched.get(row["id"])["next_run_at"])
        self.assertEqual(nxt, _dt.datetime(2026, 11, 9, 9, 0))

    def test_idempotency_key_shape(self):
        self.sched.add_once("one", run_at=200.0, goal="do thing")
        self.now[0] = 200.0
        self.sched.tick()
        self.assertEqual(len(self.created), 1)
        _, key = self.created[0]
        self.assertTrue(key.startswith("schedule:"))

    def test_remove_and_disable(self):
        sid = self.sched.add_interval("x", 5.0, goal="ping")
        self.sched.set_enabled(sid, False)
        self.now[0] = 200.0
        self.assertEqual(self.sched.tick(), 0)
        self.sched.remove(sid)
        self.assertIsNone(self.sched.get(sid))

    def test_rejects_bad_cron_at_add(self):
        with self.assertRaises(ValueError):
            self.sched.add_cron("bad", "99 * * * *", goal="x")

    def test_rejects_blank_goal(self):
        with self.assertRaises(ValueError):
            self.sched.add_once("x", run_at=200.0, goal="   ")


if __name__ == "__main__":
    unittest.main()
