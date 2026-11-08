"""P1.1 + P1.6 — reliable notification delivery + initial sinks."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from core.notify import (
    _BACKOFF_SECS,
    JournalNotifier,
    NotifyQueue,
    TerminalNotifier,
    WebhookNotifier,
)
from core import journal as _journal


def _tmp(prefix: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix))


class _CountingSink:
    """Succeeds every time, counts calls. Used as a baseline sink."""
    name = "counter"

    def __init__(self):
        self.calls: list[dict] = []

    def send(self, payload: dict) -> None:
        self.calls.append(payload)


class _FlakySink:
    """Fails the first `fail_count` calls, then succeeds."""
    name = "flaky"

    def __init__(self, fail_count: int):
        self.remaining = fail_count
        self.calls = 0

    def send(self, payload: dict) -> None:
        self.calls += 1
        if self.remaining > 0:
            self.remaining -= 1
            raise RuntimeError("transient")


class _AlwaysFailSink:
    name = "fail"

    def send(self, payload: dict) -> None:
        raise RuntimeError("down")


# ── Queue basics ──────────────────────────────────────────────────────────────

class NotifyQueueEnqueueTests(unittest.TestCase):
    def setUp(self):
        self.q = NotifyQueue(_tmp("notify-") / "n.sqlite")

    def test_enqueue_returns_id_and_persists(self):
        nid = self.q.enqueue({"kind": "task_done", "task_id": 7})
        self.assertGreater(nid, 0)
        self.assertEqual(self.q.pending_count(), 1)

    def test_dedup_key_short_circuits(self):
        a = self.q.enqueue({"kind": "task_done"}, dedup_key="task:7:done")
        b = self.q.enqueue({"kind": "task_done"}, dedup_key="task:7:done")
        self.assertEqual(a, b)
        self.assertEqual(self.q.pending_count(), 1)

    def test_null_dedup_keys_do_not_collide(self):
        a = self.q.enqueue({"kind": "a"})
        b = self.q.enqueue({"kind": "b"})
        self.assertNotEqual(a, b)
        self.assertEqual(self.q.pending_count(), 2)


# ── Delivery semantics ────────────────────────────────────────────────────────

class NotifyQueueDrainTests(unittest.TestCase):
    def setUp(self):
        self.q = NotifyQueue(_tmp("notify-") / "n.sqlite")

    def test_drain_delivers_once_on_success(self):
        sink = _CountingSink()
        self.q.register_sink(sink)
        self.q.enqueue({"message": "hello"})
        out = self.q.drain()
        self.assertEqual(out["delivered"], 1)
        self.assertEqual(out["retried"], 0)
        self.assertEqual(len(sink.calls), 1)

        # Drain again should NOT redeliver already-delivered rows.
        out = self.q.drain()
        self.assertEqual(out["delivered"], 0)
        self.assertEqual(len(sink.calls), 1)

    def test_drain_without_sinks_leaves_row_pending(self):
        self.q.enqueue({"x": 1})
        out = self.q.drain()
        self.assertEqual(out, {"delivered": 0, "retried": 0, "dead_letter": 0})
        self.assertEqual(self.q.pending_count(), 1)

    def test_flaky_sink_retries_then_succeeds(self):
        sink = _FlakySink(fail_count=2)
        self.q.register_sink(sink)
        nid = self.q.enqueue({"x": 1}, now=100.0)
        # Attempt 1 fails → retried.
        out = self.q.drain(now=100.0)
        self.assertEqual(out["retried"], 1)
        self.assertEqual(self.q.row(nid)["attempts"], 1)
        # Attempt 2 is scheduled for later — nothing happens at the same timestamp.
        out = self.q.drain(now=100.0)
        self.assertEqual(out, {"delivered": 0, "retried": 0, "dead_letter": 0})
        # Jump past the backoff for attempt 2 → fails → retried again.
        out = self.q.drain(now=100.0 + _BACKOFF_SECS[1] + 1)
        self.assertEqual(out["retried"], 1)
        # Next window → success.
        out = self.q.drain(now=100.0 + _BACKOFF_SECS[1] + _BACKOFF_SECS[2] + 1)
        self.assertEqual(out["delivered"], 1)
        self.assertEqual(self.q.row(nid)["status"], "delivered")

    def test_permanent_failure_hits_dead_letter(self):
        self.q.register_sink(_AlwaysFailSink())
        nid = self.q.enqueue({"x": 1}, now=100.0)
        # Walk through every backoff window — the last drain should transition
        # the row to dead_letter.
        running = 100.0
        for delay in _BACKOFF_SECS:
            running += delay + 1
            self.q.drain(now=running)
        self.assertEqual(self.q.row(nid)["status"], "dead_letter")
        self.assertIn("down", self.q.row(nid)["last_error"])

    def test_pending_rows_survive_restart(self):
        path = _tmp("notify-") / "n.sqlite"
        q1 = NotifyQueue(path)
        q1.enqueue({"x": 1}, dedup_key="k1")
        del q1
        q2 = NotifyQueue(path)
        sink = _CountingSink()
        q2.register_sink(sink)
        out = q2.drain()
        self.assertEqual(out["delivered"], 1)
        self.assertEqual(sink.calls[0]["x"], 1)


# ── Sinks ─────────────────────────────────────────────────────────────────────

class SinkTests(unittest.TestCase):
    def test_journal_sink_writes_event(self):
        d = _tmp("notify-journal-")
        _journal.connect(d)
        JournalNotifier().send({"kind": "hello", "message": "hi"})
        # Journal file for today should exist and contain our event.
        files = list(d.glob("*.jsonl"))
        self.assertTrue(files)
        lines = files[0].read_text(encoding="utf-8").strip().splitlines()
        record = json.loads(lines[-1])
        self.assertEqual(record["event"], "notify")
        self.assertEqual(record["message"], "hi")

    def test_terminal_sink_does_not_raise(self):
        TerminalNotifier().send({"title": "X", "message": "ok"})

    def test_webhook_sink_success(self):
        seen: dict = {}

        def fake_post(url: str, payload: dict, timeout_s: float) -> int:
            seen["url"] = url
            seen["payload"] = payload
            return 204

        WebhookNotifier(url="http://example", poster=fake_post).send({"x": 1})
        self.assertEqual(seen["url"], "http://example")
        self.assertEqual(seen["payload"], {"x": 1})

    def test_webhook_sink_raises_on_non_2xx(self):
        def fake_post(url, payload, timeout_s): return 500
        with self.assertRaises(RuntimeError):
            WebhookNotifier(url="http://example", poster=fake_post).send({})


if __name__ == "__main__":
    unittest.main()
