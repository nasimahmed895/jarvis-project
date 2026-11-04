"""
Offline tests for the research sub-agent.

We stub the web_search action, the HTTP fetcher, and the Gemini summariser
so the test is deterministic and does not touch the network or the API.
"""
from __future__ import annotations

import unittest
from unittest import mock

from actions import research as rs


class ResearchTests(unittest.TestCase):

    def test_parse_search_result_list(self):
        raw = (
            "1. RootDevs — https://rootdevs.com\n"
            "2. About — https://rootdevs.com/about\n"
            "No link on this line.\n"
        )
        items = rs._parse_search_result_list(raw, max_items=5)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["url"], "https://rootdevs.com")

    def test_needs_topic(self):
        self.assertIn("provide", rs.research({}))

    @mock.patch("actions.research._gem")
    @mock.patch("actions.research._run_search")
    def test_no_sources_reports_cleanly(self, run_search, gem):
        run_search.return_value = []
        gem.FAST = "fast"; gem.SMART = "smart"
        gem.as_json.return_value = {"queries": ["x"]}
        out = rs.research({"topic": "nothing here"})
        self.assertIn("no web results", out.lower())

    @mock.patch("actions.research._gem")
    @mock.patch("actions.research._fetch_text")
    @mock.patch("actions.research._run_search")
    def test_happy_path_builds_report(self, run_search, fetch, gem):
        run_search.return_value = [
            {"title": "T1", "url": "https://a.test"},
            {"title": "T2", "url": "https://b.test"},
        ]
        fetch.return_value = "page body with facts about things"
        gem.FAST = "fast"; gem.SMART = "smart"
        gem.as_json.return_value = {"queries": ["t1", "t2"]}
        gem.text.return_value = "## Summary\nFake summary.\n## Sources\n[1] T1 — https://a.test\n[2] T2 — https://b.test"
        out = rs.research({"topic": "things", "num_queries": 2})
        self.assertIn("## Summary", out)
        self.assertIn("T1", out)
        # Summariser got the SMART tier
        self.assertEqual(gem.text.call_args.kwargs.get("tier"), "smart")


if __name__ == "__main__":
    unittest.main(verbosity=2)
