"""P2.1 — duplicate memory detection."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from memory import memory_manager as mm


class _TmpMemory:
    """Context helper: point memory_manager at a tmp file."""
    def __enter__(self):
        self._dir = tempfile.mkdtemp(prefix="mem-dedup-")
        self._patch = mock.patch.object(
            mm, "MEMORY_PATH", Path(self._dir) / "long_term.json"
        )
        self._patch.start()
        return self

    def __exit__(self, *a):
        self._patch.stop()


class MemoryDedupTests(unittest.TestCase):
    def test_identical_same_key_updates_are_noop(self):
        with _TmpMemory():
            for _ in range(100):
                mm.update_memory({"preferences": {"color": "blue"}})
            mem = mm.load_memory()
            prefs = mem.get("preferences", {})
            # Exactly one entry.
            self.assertEqual(len(prefs), 1)
            self.assertEqual(prefs["color"]["value"], "blue")

    def test_different_key_same_value_is_skipped(self):
        with _TmpMemory():
            mm.update_memory({"preferences": {"color": "blue"}})
            mm.update_memory({"preferences": {"favorite_color": "blue"}})
            prefs = mm.load_memory().get("preferences", {})
            self.assertEqual(list(prefs.keys()), ["color"])

    def test_different_key_different_value_coexists(self):
        with _TmpMemory():
            mm.update_memory({"preferences": {"color": "blue"}})
            mm.update_memory({"preferences": {"favorite_color": "red"}})
            prefs = mm.load_memory().get("preferences", {})
            self.assertEqual(set(prefs.keys()), {"color", "favorite_color"})

    def test_whitespace_and_case_variants_dedupe(self):
        with _TmpMemory():
            mm.update_memory({"preferences": {"drink": "Coffee"}})
            mm.update_memory({"preferences": {"beverage": "  coffee  "}})
            prefs = mm.load_memory().get("preferences", {})
            self.assertEqual(list(prefs.keys()), ["drink"])

    def test_dedup_is_scoped_per_category(self):
        """A duplicate value in a different category is not dropped."""
        with _TmpMemory():
            mm.update_memory({"preferences": {"color": "blue"}})
            mm.update_memory({"notes": {"last_answer": "blue"}})
            mem = mm.load_memory()
            self.assertIn("color", mem["preferences"])
            self.assertIn("last_answer", mem["notes"])


if __name__ == "__main__":
    unittest.main()
