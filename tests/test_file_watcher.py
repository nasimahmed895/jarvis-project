"""P2.5 — file change monitor."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from actions import file_watcher
from actions.file_watcher import FileWatcher


def _tmp(prefix: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix))


class FileWatcherPollingTests(unittest.TestCase):
    def setUp(self):
        self.root = _tmp("watch-")
        self.events: list[dict] = []
        self.w = FileWatcher(self.root, name="t", event_cb=self.events.append)
        self.w._refresh(emit=False)   # seed snapshot

    def test_created_event_fires(self):
        (self.root / "new.txt").write_text("hi", encoding="utf-8")
        self.w.poll()
        kinds = [e["event"] for e in self.events]
        self.assertIn("created", kinds)

    def test_modified_event_fires(self):
        f = self.root / "x.txt"
        f.write_text("1", encoding="utf-8")
        self.w.poll()
        self.events.clear()
        # Force a different mtime even on fast-clock filesystems.
        f.write_text("2", encoding="utf-8")
        os.utime(f, (time.time() + 1, time.time() + 1))
        self.w.poll()
        self.assertTrue(any(e["event"] == "modified" for e in self.events))

    def test_deleted_event_fires(self):
        f = self.root / "x.txt"
        f.write_text("1", encoding="utf-8")
        self.w.poll()
        self.events.clear()
        f.unlink()
        self.w.poll()
        kinds = [e["event"] for e in self.events]
        self.assertEqual(kinds, ["deleted"])

    def test_pattern_filter(self):
        w = FileWatcher(self.root, name="log-only",
                        patterns=["*.log"], event_cb=self.events.append)
        w._refresh(emit=False)
        (self.root / "a.txt").write_text("x", encoding="utf-8")
        (self.root / "a.log").write_text("x", encoding="utf-8")
        w.poll()
        paths = [e["path"] for e in self.events]
        self.assertTrue(any(p.endswith("a.log") for p in paths))
        self.assertFalse(any(p.endswith("a.txt") for p in paths))

    def test_recursive_walk(self):
        (self.root / "sub").mkdir()
        self.w._refresh(emit=False)
        (self.root / "sub" / "deep.txt").write_text("x", encoding="utf-8")
        self.w.poll()
        self.assertTrue(any(e["path"].endswith("deep.txt") for e in self.events))


class FileWatcherRegistryTests(unittest.TestCase):
    def setUp(self):
        self.root = _tmp("watch-reg-")
        # Clear any watchers left by other tests.
        for name in list(file_watcher._REGISTRY):
            file_watcher.unregister(name)

    def tearDown(self):
        for name in list(file_watcher._REGISTRY):
            file_watcher.unregister(name)

    def test_register_and_list(self):
        r = file_watcher._tool_entry({
            "action": "add", "name": "x",
            "path": str(self.root), "interval_s": 60.0,
        })
        self.assertIn("Watching", r)
        listing = file_watcher._tool_entry({"action": "list"})
        self.assertIn("x", listing)

    def test_remove(self):
        file_watcher._tool_entry({
            "action": "add", "name": "x",
            "path": str(self.root), "interval_s": 60.0,
        })
        r = file_watcher._tool_entry({"action": "remove", "name": "x"})
        self.assertIn("Stopped", r)
        self.assertEqual(file_watcher.list_watchers(), [])

    def test_add_without_path(self):
        r = file_watcher._tool_entry({"action": "add", "name": "x"})
        self.assertIn("path", r)


if __name__ == "__main__":
    unittest.main()
