"""Tests for actions/form_fill.py — browser_control delegation stubbed."""
from __future__ import annotations

from pathlib import Path

import pytest

from actions import form_fill
from memory import user_profile
from core import confirm


@pytest.fixture(autouse=True)
def _tmp_profile(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(user_profile, "_PATH", tmp_path / "user_profile.json")
    # Clean confirm state — no HUD in tests.
    confirm._show_cb = None
    confirm._hide_cb = None
    yield


@pytest.fixture
def stub_bc(monkeypatch):
    calls: list[dict] = []

    class _FakeBC:
        @staticmethod
        def browser_control(parameters=None, **kw):
            calls.append(parameters or {})
            action = (parameters or {}).get("action")
            if action == "go_to":
                return "Navigated."
            if action == "fill_form":
                return "Filled 3; missed 0."
            if action == "click":
                return "Clicked Submit."
            return "ok"

    monkeypatch.setattr(form_fill, "_bc", _FakeBC)
    return calls


def test_refuses_when_required_missing(stub_bc):
    out = form_fill.form_fill({"kind": "registration",
                               "url": "https://example.test/signup"})
    # Did not call fill_form because required is missing.
    assert not any(c.get("action") == "fill_form" for c in stub_bc)
    assert "missing" in out.lower()


def test_fills_when_required_present(stub_bc):
    user_profile.set("first_name", "Ada")
    user_profile.set("last_name", "Lovelace")
    user_profile.set("email", "ada@example.com")
    out = form_fill.form_fill({"kind": "registration",
                               "url": "https://example.test/signup"})
    # fill_form was invoked with our values
    fills = [c for c in stub_bc if c.get("action") == "fill_form"]
    assert fills, "fill_form should have been called"
    fields = fills[0].get("fields", {})
    assert fields.get("first_name") == "Ada"
    assert fields.get("email") == "ada@example.com"
    assert "approve submit" in out.lower()


def test_submit_after_is_gated_lvl2_passthrough_no_hud(stub_bc):
    user_profile.set("first_name", "Ada")
    user_profile.set("email", "ada@example.com")
    user_profile.set("last_name", "x")
    out = form_fill.form_fill({"kind": "registration",
                               "url": "https://example.test/signup",
                               "submit_after": True,
                               "submit_label": "Create account"})
    # LVL2 + no HUD bound → gate falls through to run(); click fires.
    click_calls = [c for c in stub_bc if c.get("action") == "click"]
    assert click_calls, "submit click should have fired"
    assert click_calls[0].get("text") == "Create account"


def test_ignore_missing_lets_partial_fill(stub_bc):
    user_profile.set("first_name", "Ada")
    out = form_fill.form_fill({"kind": "registration",
                               "url": "https://example.test/signup",
                               "ignore_missing": True})
    fills = [c for c in stub_bc if c.get("action") == "fill_form"]
    assert fills, "partial fill should still invoke fill_form"
    assert fills[0]["fields"].get("first_name") == "Ada"


def test_unknown_kind_rejected():
    out = form_fill.form_fill({"kind": "not_a_kind"})
    assert "Unknown kind" in out
