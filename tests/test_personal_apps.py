"""
Tests for calendar / contacts / email actions.

We don't spawn Mail/Calendar/Contacts in the test runner — AppleScript
is monkey-patched with stubs so each branch is exercised without touching
the user's real data or requiring macOS Automation grants.
"""
from __future__ import annotations

import unittest
from unittest import mock

from actions.calendar_manager import calendar_manager
from actions.contacts_manager import contacts_manager
from actions.email_manager import email_manager


class CalendarTests(unittest.TestCase):
    def test_add_requires_fields(self):
        self.assertIn("provide",
                      calendar_manager({"action": "add"}))

    @mock.patch("actions._mac_apps.calendar_create_event",
                return_value="Added to Calendar.")
    def test_add_calls_bridge(self, m):
        out = calendar_manager({
            "action": "add",
            "title": "Dentist",
            "start": "2026-11-15 10:00",
            "duration_min": 30,
        })
        m.assert_called_once()
        self.assertIn("Added to Calendar", out)

    @mock.patch("actions._mac_apps.calendar_list_events",
                return_value=[
                    {"title": "Standup", "start": "2026-11-05 10:00",
                     "end": "2026-11-05 10:30", "location": "",
                     "calendar": "Work"},
                ])
    def test_list_renders(self, m):
        out = calendar_manager({"action": "list", "days": 3})
        self.assertIn("Standup", out)
        self.assertIn("Work", out)

    @mock.patch("actions._mac_apps.calendar_list_events")
    def test_permission_denied_maps_cleanly(self, m):
        from actions._mac_apps import AppleScriptError
        m.side_effect = AppleScriptError("-1743 Not authorised")
        out = calendar_manager({"action": "list"})
        self.assertIn("Automation", out)


class ContactsTests(unittest.TestCase):
    @mock.patch("actions._mac_apps.contacts_search",
                return_value=[
                    {"name": "Alice Smith",
                     "emails": ["alice@example.com"],
                     "phones": ["+1 555 1234"]},
                ])
    def test_search_renders(self, m):
        out = contacts_manager({"action": "search", "query": "Alice"})
        self.assertIn("Alice Smith", out)
        self.assertIn("alice@example.com", out)

    def test_search_needs_query(self):
        self.assertIn("provide",
                      contacts_manager({"action": "search"}))


class EmailTests(unittest.TestCase):
    @mock.patch("actions._mac_apps.mail_list_recent",
                return_value=[
                    {"id": "123", "from": "a@b.com",
                     "subject": "Hello", "date": "2026-11-05",
                     "unread": True},
                ])
    def test_list_renders(self, m):
        out = email_manager({"action": "list", "mailbox": "INBOX"})
        self.assertIn("Hello", out)
        self.assertIn("a@b.com", out)
        self.assertIn("●", out)

    @mock.patch("actions._mac_apps.mail_read",
                return_value={"subject": "Hi", "from": "x@y.com",
                              "body": "Body text"})
    def test_read_renders(self, m):
        out = email_manager({"action": "read", "id": "123"})
        self.assertIn("Subject: Hi", out)
        self.assertIn("Body text", out)

    def test_send_requires_fields(self):
        self.assertIn("provide",
                      email_manager({"action": "send"}))

    @mock.patch("actions.email_manager._trust", return_value=True)
    @mock.patch("actions._mac_apps.mail_compose",
                return_value="Email sent to x@y.com: Hi")
    def test_send_bypasses_gate_with_trust(self, compose, _t):
        out = email_manager({
            "action": "send", "to": "x@y.com",
            "subject": "Hi", "body": "msg",
        })
        compose.assert_called_once()
        self.assertEqual(compose.call_args.kwargs.get("send"), True)
        self.assertIn("sent", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
