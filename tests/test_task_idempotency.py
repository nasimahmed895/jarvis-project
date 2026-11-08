"""P1.2 — duplicate-execution prevention via idempotency_key."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core import tasks


def _tmp(prefix: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix))


class IdempotencyKeyTests(unittest.TestCase):
    def setUp(self):
        base = _tmp("tasks-idemp-")
        tasks.connect(base / "tasks.sqlite")

    def test_same_key_returns_same_id(self):
        a = tasks.create_task("do X", idempotency_key="k1")
        b = tasks.create_task("do X", idempotency_key="k1")
        self.assertEqual(a, b)
        # Only one row exists under that key.
        self.assertEqual(len(tasks.list_tasks()), 1)

    def test_different_keys_create_distinct_tasks(self):
        a = tasks.create_task("do X", idempotency_key="k1")
        b = tasks.create_task("do Y", idempotency_key="k2")
        self.assertNotEqual(a, b)
        self.assertEqual(len(tasks.list_tasks()), 2)

    def test_completed_key_still_dedupes(self):
        a = tasks.create_task("done thing", idempotency_key="shared")
        tasks.set_task_status(a, "done", result="ok")
        b = tasks.create_task("done thing", idempotency_key="shared")
        self.assertEqual(a, b)
        self.assertEqual(tasks.get_task(a)["status"], "done")

    def test_null_keys_do_not_collide(self):
        """Partial-unique index means two unkeyed tasks coexist."""
        a = tasks.create_task("free 1")
        b = tasks.create_task("free 2")
        self.assertNotEqual(a, b)

    def test_cancelled_key_still_short_circuits(self):
        """Cancelled tasks still occupy their key — rerun requires a new key."""
        a = tasks.create_task("try", idempotency_key="only-once")
        tasks.cancel_task(a)
        b = tasks.create_task("try again", idempotency_key="only-once")
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
