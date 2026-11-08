"""P4.11 — predictive task suggestions."""

from __future__ import annotations

import datetime as _dt
import json
import tempfile
import unittest
from pathlib import Path

from core import predictor


def _tmp(prefix: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix))


def _write_day(directory: Path, day: _dt.date, events: list[dict]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{day.isoformat()}.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e) + "\n")


def _event(goal: str, at: _dt.datetime, kind: str = "task_done") -> dict:
    return {"ts": at.astimezone().isoformat(), "event": kind, "goal": goal}


class PredictorHistoryTests(unittest.TestCase):
    def test_read_history_filters_by_window(self):
        d = _tmp("pred-")
        today = _dt.date(2026, 11, 8)
        _write_day(d, today, [_event("x", _dt.datetime(2026, 11, 8, 9))])
        _write_day(d, today - _dt.timedelta(days=60),
                   [_event("ancient", _dt.datetime(2026, 9, 9, 9))])
        rows = predictor.read_history(days=30, directory=d)
        goals = {r["goal"] for r in rows}
        self.assertIn("x", goals)
        self.assertNotIn("ancient", goals)


class PredictorSuggestionsTests(unittest.TestCase):
    def test_daily_pattern_detected(self):
        d = _tmp("pred-")
        today = _dt.date(2026, 11, 8)
        # morning brief every day at 09:00 for a week.
        for offset in range(7):
            day = today - _dt.timedelta(days=offset)
            _write_day(d, day, [_event(
                "morning brief",
                _dt.datetime(day.year, day.month, day.day, 9, 2),
            )])
        suggestions = predictor.suggest(directory=d, today=today)
        self.assertTrue(suggestions)
        top = suggestions[0]
        self.assertEqual(top["kind"], "daily")
        self.assertEqual(top["slot"], "09:02")
        self.assertEqual(top["goal"], "morning brief")
        self.assertEqual(top["cron"], "2 9 * * *")
        self.assertGreaterEqual(top["support"], 3)

    def test_weekly_pattern_detected(self):
        d = _tmp("pred-")
        today = _dt.date(2026, 11, 8)   # Sunday
        # Every Monday at 15:00 for 5 weeks.
        for w in range(5):
            # Find the Monday of (today - w weeks).
            monday = today - _dt.timedelta(days=(today.weekday() + 7 * w))
            _write_day(d, monday, [_event(
                "weekly review",
                _dt.datetime(monday.year, monday.month, monday.day, 15, 0),
            )])
        suggestions = predictor.suggest(directory=d, today=today)
        best = suggestions[0]
        self.assertEqual(best["kind"], "weekly")
        self.assertTrue(best["slot"].startswith("Mon@"))
        # cron DOW: Monday → 1
        self.assertTrue(best["cron"].endswith(" 1"))

    def test_support_below_threshold_rejected(self):
        d = _tmp("pred-")
        today = _dt.date(2026, 11, 8)
        for offset in (0, 1):
            day = today - _dt.timedelta(days=offset)
            _write_day(d, day, [_event(
                "rare thing", _dt.datetime(day.year, day.month, day.day, 10),
            )])
        self.assertEqual(predictor.suggest(directory=d, today=today), [])

    def test_wide_spread_rejected(self):
        d = _tmp("pred-")
        today = _dt.date(2026, 11, 8)
        hours = [3, 11, 20]   # too scattered to form a daily slot.
        for i, h in enumerate(hours):
            day = today - _dt.timedelta(days=i)
            _write_day(d, day, [_event(
                "random thing",
                _dt.datetime(day.year, day.month, day.day, h, 0),
            )])
        # At best one weekday has a single sample → below threshold → nothing.
        self.assertEqual(predictor.suggest(directory=d, today=today), [])

    def test_empty_directory_returns_empty(self):
        self.assertEqual(predictor.suggest(directory=_tmp("pred-empty-")), [])


if __name__ == "__main__":
    unittest.main()
