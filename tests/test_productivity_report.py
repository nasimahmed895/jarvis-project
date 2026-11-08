"""P3.2 — productivity analytics rollup."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from core import tasks
from actions import habit, productivity_report as pr


def _tmp(prefix: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix))


class RollupTests(unittest.TestCase):
    def setUp(self):
        tasks._SUBSCRIBERS.clear()
        base = _tmp("prod-")
        tasks.connect(base / "tasks.sqlite")
        habit._reset_connection_for_tests(base / "habits.sqlite")

    def tearDown(self):
        tasks._SUBSCRIBERS.clear()
        habit._reset_connection_for_tests()

    def test_rejects_unknown_window(self):
        with self.assertRaises(ValueError):
            pr.rollup("yearly")

    def test_counts_tasks_within_window(self):
        tasks.create_task("a")
        tasks.create_task("b")
        stats = pr.rollup("week")
        self.assertEqual(stats["tasks"]["created"], 2)
        self.assertEqual(stats["tasks"]["by_status"]["planned"], 2)
        self.assertEqual(stats["tasks"]["completion_pct"], 0.0)

    def test_completion_percent_reflects_done(self):
        a = tasks.create_task("a"); tasks.set_task_status(a, "done", result="ok")
        b = tasks.create_task("b")
        stats = pr.rollup("week")
        self.assertEqual(stats["tasks"]["completion_pct"], 50.0)

    def test_step_duration_rollup(self):
        tid = tasks.create_task("g")
        s1 = tasks.add_step(tid, 1, "v", "speak", {}, [])
        tasks.set_step_status(s1, "running")
        time.sleep(0.02)
        tasks.set_step_status(s1, "done", result="ok")
        stats = pr.rollup("week")
        self.assertEqual(stats["tasks"]["steps_timed"], 1)
        self.assertGreater(stats["tasks"]["avg_step_s"], 0.0)

    def test_old_tasks_excluded_from_window(self):
        tasks.create_task("ancient")
        stats = pr.rollup("week", now=time.time() + 60 * 60 * 24 * 30)
        self.assertEqual(stats["tasks"]["created"], 0)

    def test_habits_section_populated(self):
        habit.add("x", "daily")
        stats = pr.rollup("week")
        self.assertEqual(stats["habits"][0]["name"], "x")

    def test_render_markdown_contains_known_sections(self):
        tid = tasks.create_task("x"); tasks.set_task_status(tid, "done", result="ok")
        habit.add("sleep", "daily")
        out = pr.render_markdown(pr.rollup("week"))
        self.assertIn("# Productivity rollup", out)
        self.assertIn("## Tasks", out)
        self.assertIn("## Habits", out)
        self.assertIn("sleep", out)

    def test_tool_entry_default_window_is_week(self):
        tasks.create_task("x")
        out = pr._tool_entry({})
        self.assertIn("week", out.splitlines()[0])

    def test_tool_entry_rejects_bad_window(self):
        self.assertIn("window", pr._tool_entry({"window": "yearly"}))


if __name__ == "__main__":
    unittest.main()
