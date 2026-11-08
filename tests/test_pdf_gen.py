"""P2.3 — markdown → PDF generator."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from actions import pdf_gen


def _tmp(prefix: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix))


@unittest.skipIf(not pdf_gen._HAS_REPORTLAB, "reportlab not installed")
class PdfGenRealTests(unittest.TestCase):
    def test_renders_markdown_to_file(self):
        dst = _tmp("pdf-") / "out.pdf"
        result = pdf_gen.generate("# Hello\n\nBody text.\n\n**bold** and *italic*.", dst)
        self.assertTrue(dst.exists())
        self.assertGreater(dst.stat().st_size, 200)
        self.assertIn("Wrote", result)
        # Check PDF magic bytes.
        self.assertEqual(dst.read_bytes()[:4], b"%PDF")

    def test_tool_entry_inline(self):
        dst = _tmp("pdf-") / "out.pdf"
        r = pdf_gen._tool_entry({"content": "# Report", "dst_path": str(dst)})
        self.assertIn("Wrote", r)
        self.assertTrue(dst.exists())

    def test_tool_entry_reads_src_path(self):
        d = _tmp("pdf-")
        src = d / "note.md"
        src.write_text("# From File\nBody", encoding="utf-8")
        r = pdf_gen._tool_entry({"src_path": str(src)})
        self.assertIn("Wrote", r)
        self.assertTrue((d / "note.pdf").exists())


class PdfGenMissingDepTests(unittest.TestCase):
    """Behavior when reportlab is not available — simulated via flag."""

    def test_returns_install_hint_when_unavailable(self):
        orig = pdf_gen._HAS_REPORTLAB
        pdf_gen._HAS_REPORTLAB = False
        try:
            r = pdf_gen.generate("# x", _tmp("pdf-") / "out.pdf")
            self.assertIn("reportlab", r)
            self.assertIn("pip install", r)
        finally:
            pdf_gen._HAS_REPORTLAB = orig


class PdfGenAdapterTests(unittest.TestCase):
    def test_tool_entry_rejects_empty_content(self):
        r = pdf_gen._tool_entry({"content": "   ", "dst_path": "/tmp/x.pdf"})
        self.assertIn("No content", r)

    def test_tool_entry_requires_dst_when_inline(self):
        r = pdf_gen._tool_entry({"content": "body"})
        self.assertIn("dst_path", r)


if __name__ == "__main__":
    unittest.main()
