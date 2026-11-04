"""Tests for the todo list manager. All writes go to the production
`memory/todos.sqlite`; we use a disposable list name per test to isolate."""
from __future__ import annotations

import unittest
import uuid

from actions.todo import todo


class TodoTests(unittest.TestCase):
    def setUp(self):
        self.list = f"test-{uuid.uuid4().hex[:8]}"

    def tearDown(self):
        todo({"action": "clear", "list": self.list})

    def test_add_and_list(self):
        todo({"action": "add", "list": self.list, "text": "buy milk"})
        todo({"action": "add", "list": self.list, "text": "call mum",
              "priority": 2})
        out = todo({"action": "list", "list": self.list})
        self.assertIn("buy milk", out)
        self.assertIn("!!call mum", out)

    def test_done_undo_delete(self):
        add = todo({"action": "add", "list": self.list, "text": "item A"})
        tid = int(add.split("#")[1].split(" ")[0])
        self.assertIn("done", todo({"action": "done", "id": tid}))
        open_only = todo({"action": "list", "list": self.list})
        self.assertNotIn(f"#{tid}", open_only)
        self.assertIn(f"#{tid}",
                      todo({"action": "list", "list": self.list, "status": "done"}))
        todo({"action": "undo", "id": tid})
        self.assertIn(f"#{tid}",
                      todo({"action": "list", "list": self.list}))
        todo({"action": "delete", "id": tid})
        self.assertIn("empty", todo({"action": "list", "list": self.list}))

    def test_search_across_lists(self):
        todo({"action": "add", "list": self.list, "text": "pick up eggs"})
        out = todo({"action": "search", "query": "eggs"})
        self.assertIn("eggs", out)

    def test_bad_input(self):
        self.assertIn("provide", todo({"action": "add", "list": self.list}))
        self.assertIn("provide", todo({"action": "done"}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
