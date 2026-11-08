"""P3.5 — dashboard responsive styling guardrails."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

APP_HTML = Path(__file__).resolve().parents[1] / "dashboard" / "static" / "app.html"


class ResponsiveMarkupTests(unittest.TestCase):
    def setUp(self):
        self.html = APP_HTML.read_text(encoding="utf-8")

    def test_viewport_meta_present(self):
        self.assertIn('name="viewport"', self.html)
        self.assertRegex(self.html, r"width=device-width")

    def test_mobile_breakpoint_present(self):
        self.assertIn("max-width: 900px", self.html)
        self.assertIn("max-width: 640px", self.html)

    def test_grid_collapse_rule_present(self):
        """At 640px wide, any Tailwind grid-cols-* should collapse to 1 col."""
        block = _extract_media_block(self.html, "max-width: 640px")
        self.assertRegex(block, r'grid-template-columns:\s*1fr\s*!important')

    def test_tables_scroll_horizontally_on_mobile(self):
        block = _extract_media_block(self.html, "max-width: 640px")
        self.assertIn("overflow-x:auto", block)

    def test_safe_area_inset_honored(self):
        block = _extract_media_block(self.html, "max-width: 640px")
        self.assertIn("safe-area-inset", block)

    def test_sidebar_width_shrinks_on_tiny_screens(self):
        block = _extract_media_block(self.html, "max-width: 420px")
        self.assertIn("vw", block.lower())


def _extract_media_block(html: str, query: str) -> str:
    m = re.search(rf"@media\s*\(\s*{re.escape(query)}\s*\)\s*\{{", html)
    assert m, f"media query not found: {query}"
    # Walk braces to find the matching closing brace of this media block.
    i = m.end() - 1
    depth = 0
    start = i
    while i < len(html):
        c = html[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return html[start:i + 1]
        i += 1
    raise AssertionError("unbalanced braces in media block")


if __name__ == "__main__":
    unittest.main()
