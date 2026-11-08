"""P3.1 — habit tracker."""

from __future__ import annotations

import datetime as _dt
import tempfile
import unittest
from pathlib import Path

from actions import habit


def _tmp(prefix: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix))


class HabitTests(unittest.TestCase):
    def setUp(self):
        habit._reset_connection_for_tests(_tmp("habit-") / "habits.sqlite")

    def tearDown(self):
        habit._reset_connection_for_tests()

    def test_add_and_list(self):
        habit.add("Read", "daily")
        rows = habit.list_habits()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "Read")

    def test_duplicate_name_rejected(self):
        habit.add("x", "daily")
        with self.assertRaises(Exception):
            habit.add("x", "daily")

    def test_invalid_cadence_rejected(self):
        with self.assertRaises(ValueError):
            habit.add("x", "sometimes")

    def test_daily_streak_three_days(self):
        habit.add("exercise", "daily")
        today = _dt.date(2026, 11, 8)
        habit.mark_done("exercise", on=today - _dt.timedelta(days=2))
        habit.mark_done("exercise", on=today - _dt.timedelta(days=1))
        habit.mark_done("exercise", on=today)
        self.assertEqual(habit.streak("exercise", on=today), 3)

    def test_daily_streak_breaks_on_gap(self):
        habit.add("x", "daily")
        today = _dt.date(2026, 11, 8)
        habit.mark_done("x", on=today - _dt.timedelta(days=3))
        habit.mark_done("x", on=today - _dt.timedelta(days=1))
        habit.mark_done("x", on=today)
        self.assertEqual(habit.streak("x", on=today), 2)

    def test_mark_done_is_idempotent_per_day(self):
        habit.add("x", "daily")
        today = _dt.date(2026, 11, 8)
        a = habit.mark_done("x", on=today)
        b = habit.mark_done("x", on=today)
        self.assertTrue(a["new"])
        self.assertFalse(b["new"])
        self.assertEqual(habit.streak("x", on=today), 1)

    def test_weekly_streak_counts_iso_weeks(self):
        habit.add("review", "weekly")
        week1 = _dt.date(2026, 10, 26)   # ISO week 44, Monday
        week2 = _dt.date(2026, 11, 2)    # ISO week 45
        week3 = _dt.date(2026, 11, 9)    # ISO week 46
        habit.mark_done("review", on=week1)
        habit.mark_done("review", on=week2)
        habit.mark_done("review", on=week3)
        self.assertEqual(habit.streak("review", on=week3), 3)

    def test_every_n_cadence(self):
        habit.add("deep-work", "every:3")
        today = _dt.date(2026, 11, 8)
        for days_back in (6, 3, 0):
            habit.mark_done("deep-work", on=today - _dt.timedelta(days=days_back))
        self.assertEqual(habit.streak("deep-work", on=today), 3)

    def test_remove(self):
        habit.add("x", "daily")
        self.assertEqual(habit.remove("x"), {"removed": True, "habit": "x"})
        self.assertEqual(habit.list_habits(), [])
        self.assertEqual(habit.remove("x"), {"removed": False})

    def test_streak_unknown_raises(self):
        with self.assertRaises(KeyError):
            habit.streak("ghost")

    def test_tool_entry_add_and_list(self):
        self.assertIn("Added", habit._tool_entry({"action": "add", "name": "sleep", "cadence": "daily"}))
        listing = habit._tool_entry({"action": "list"})
        self.assertIn("sleep", listing)

    def test_tool_entry_unknown_action(self):
        self.assertIn("Unknown action", habit._tool_entry({"action": "nope"}))


if __name__ == "__main__":
    unittest.main()
