"""Tests for the interface-driven confirmation gate."""
from __future__ import annotations

import threading

from core import confirm


def test_resolve_notifies_jarvis_when_user_confirms():
    notified = []
    completed = threading.Event()
    confirm.bind(
        show=lambda *_: None,
        hide=lambda: None,
        resolved=lambda title, accepted: notified.append((title, accepted)),
    )
    try:
        confirm.request(
            key="test",
            title="Restart the computer",
            detail="",
            run=lambda: completed.set() or "Restarting.",
        )
        confirm.resolve(True)

        assert notified == [("Restart the computer", True)]
        assert completed.wait(timeout=1)
    finally:
        confirm.bind(show=None, hide=None)


def test_resolve_notifies_jarvis_when_user_cancels():
    notified = []
    confirm.bind(
        show=lambda *_: None,
        hide=lambda: None,
        resolved=lambda title, accepted: notified.append((title, accepted)),
    )
    try:
        confirm.request(
            key="test",
            title="Delete the file",
            detail="",
            run=lambda: "Deleted.",
        )
        confirm.resolve(False)

        assert notified == [("Delete the file", False)]
    finally:
        confirm.bind(show=None, hide=None)
