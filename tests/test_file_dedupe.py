"""P2.4 — duplicate file detection."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from actions import file_dedupe


def _tmp(prefix: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix))


class FileDedupeTests(unittest.TestCase):
    def test_finds_identical_files(self):
        root = _tmp("dedupe-")
        (root / "a.txt").write_text("hello world", encoding="utf-8")
        (root / "b.txt").write_text("hello world", encoding="utf-8")
        (root / "c.txt").write_text("hello world", encoding="utf-8")
        (root / "unique.txt").write_text("different", encoding="utf-8")
        groups = file_dedupe.scan(root)
        self.assertEqual(len(groups), 1)
        g = groups[0]
        self.assertEqual(len(g["paths"]), 3)
        self.assertEqual(g["size"], len("hello world"))

    def test_recursive_walk(self):
        root = _tmp("dedupe-r-")
        (root / "sub").mkdir()
        (root / "a.txt").write_text("same", encoding="utf-8")
        (root / "sub" / "b.txt").write_text("same", encoding="utf-8")
        self.assertEqual(len(file_dedupe.scan(root, recursive=True)), 1)
        self.assertEqual(len(file_dedupe.scan(root, recursive=False)), 0)

    def test_different_sizes_never_hashed(self):
        """Mixing distinct sizes should never generate a duplicate group."""
        root = _tmp("dedupe-s-")
        (root / "a.txt").write_text("hi", encoding="utf-8")
        (root / "b.txt").write_text("hello", encoding="utf-8")
        (root / "c.txt").write_text("hi there", encoding="utf-8")
        self.assertEqual(file_dedupe.scan(root), [])

    def test_min_bytes_filter(self):
        root = _tmp("dedupe-mb-")
        (root / "a.txt").write_text("x", encoding="utf-8")
        (root / "b.txt").write_text("x", encoding="utf-8")
        self.assertEqual(file_dedupe.scan(root, min_bytes=100), [])
        self.assertEqual(len(file_dedupe.scan(root, min_bytes=0)), 1)

    def test_tool_entry_output(self):
        root = _tmp("dedupe-t-")
        (root / "a.txt").write_text("same", encoding="utf-8")
        (root / "b.txt").write_text("same", encoding="utf-8")
        r = file_dedupe._tool_entry({"root": str(root)})
        self.assertIn("duplicate group", r)
        self.assertIn("a.txt", r)
        self.assertIn("Reclaimable", r)

    def test_tool_entry_missing_root(self):
        r = file_dedupe._tool_entry({})
        self.assertIn("root", r)

    def test_tool_entry_nonexistent_path(self):
        r = file_dedupe._tool_entry({"root": "/tmp/__never__exists__"})
        self.assertIn("not found", r.lower())


if __name__ == "__main__":
    unittest.main()
