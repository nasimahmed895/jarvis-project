"""Tests for core/approval.py — the 3-level sensitivity classifier."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core import approval, confirm


def test_lvl1_read_only_browser_action():
    cls = approval.classify("browser_control", {"action": "go_to",
                                                "url": "https://example.com"})
    assert cls.level == 1


def test_lvl3_financial():
    assert approval.classify("payment", {}).level == 3


def test_lvl3_browser_start_debug_quits_running_browser():
    assert approval.classify("browser_control",
                             {"action": "start_debug"}).level == 3


def test_unknown_defaults_to_lvl2():
    cls = approval.classify("some_unknown_tool", {})
    assert cls.level == 2


def test_lvl1_runs_without_prompt():
    calls = []

    def run():
        calls.append("ran")
        return "ok"

    out = approval.gate("web_search", {},
                        title="t", detail="d", run=run)
    assert out == "ok"
    assert calls == ["ran"]


def test_lvl3_raises_banner_when_hud_bound(monkeypatch):
    shown: list[tuple[str, str]] = []

    def _show(title: str, detail: str):
        shown.append((title, detail))

    confirm.bind(show=_show, hide=lambda: None)
    try:
        ran = []

        def run():
            ran.append("x")
            return "fired"

        msg = approval.gate("payment", {},
                            title="Pay $42", detail="Vendor X",
                            run=run)
        assert "CONFIRMATION_PENDING" in msg
        # run must NOT have fired until the UI resolves
        assert ran == []
        assert shown == [("Pay $42", "Vendor X")]
    finally:
        confirm.bind(show=None, hide=None)  # reset
        confirm._show_cb = None
        confirm._hide_cb = None


def test_lvl2_without_hud_runs_through(monkeypatch):
    # In headless/test context the UI is not bound; LVL2 falls through so
    # the loop keeps moving instead of deadlocking.
    confirm._show_cb = None
    confirm._hide_cb = None
    calls = []

    def run():
        calls.append("ran")
        return "ok"

    out = approval.gate("some_lvl2_tool", {},
                        title="t", detail="d", run=run)
    assert out == "ok"
    assert calls == ["ran"]


def test_lvl2_auto_pref_toggle(tmp_path: Path, monkeypatch):
    prefs_file = tmp_path / "approval.json"
    monkeypatch.setattr(approval, "_PREFS_PATH", prefs_file)

    assert approval.lvl2_auto("email_draft") is False
    approval.set_lvl2_auto("email_draft", True)
    assert approval.lvl2_auto("email_draft") is True
    approval.set_lvl2_auto("email_draft", False)
    assert approval.lvl2_auto("email_draft") is False
    # File shape
    data = json.loads(prefs_file.read_text(encoding="utf-8"))
    assert data == {"email_draft": False}
