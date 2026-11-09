"""
Tests for the fixes to actions/browser_control.py.

Uses tiny fake Playwright objects instead of starting a real browser;
that keeps the test fast and deterministic while exercising the exact
state-management paths that produced the bugs we fixed:
  * context.new_page() crashes after context close
  * active page replaced with a closed sibling after tab ops
  * click by text-only with no role/label fallback
  * fill_form fabricating success without reading values back
  * switch_tab leaving self._page pointing at a closed page

We import _BrowserSession and drive it through its helpers; the real
Playwright attach / launch paths are not exercised here (they need a
real Chromium binary — that belongs in tests/integration/).
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from actions import browser_control as bc
from core import synonyms


# ── Fake Playwright primitives ─────────────────────────────────────────

class _FakeLocator:
    def __init__(self, exists: bool = True, value: str = "",
                 input_type: str | None = None,
                 attrs: dict[str, str] | None = None,
                 click_cb=None):
        self._exists = exists
        self._value = value
        self._type = input_type
        self._attrs = attrs or {}
        self._click_cb = click_cb
        self.first = self  # chainable .first

    async def count(self):
        return 1 if self._exists else 0

    async def wait_for(self, state="visible", timeout=4_000):
        return None

    async def fill(self, v):
        if not self._exists:
            raise RuntimeError("no element")
        self._value = v

    async def click(self, timeout=None):
        if not self._exists:
            raise RuntimeError("no element")
        if self._click_cb:
            self._click_cb()

    async def type(self, v, delay=0):
        self._value = v

    async def input_value(self):
        return self._value

    async def get_attribute(self, name):
        if name == "type":
            return self._type
        return self._attrs.get(name)


class _FakePage:
    def __init__(self, url="about:blank"):
        self.url = url
        self._closed = False
        # map (selector_kind, label) -> locator; defaults to not-found.
        self.locators: dict[tuple[str, str], _FakeLocator] = {}

    def is_closed(self):
        return self._closed

    async def title(self):
        return f"Title({self.url})"

    async def bring_to_front(self):
        pass

    async def close(self):
        self._closed = True

    # Playwright selector APIs — each returns a locator from the table.
    def get_by_role(self, role, name=None, exact=False):
        return self.locators.get(("role", f"{role}:{name}"),
                                 _FakeLocator(exists=False))

    def get_by_label(self, name, exact=False):
        return self.locators.get(("label", name),
                                 _FakeLocator(exists=False))

    def get_by_placeholder(self, name, exact=False):
        return self.locators.get(("placeholder", name),
                                 _FakeLocator(exists=False))

    def get_by_text(self, name, exact=False):
        key = "text:exact" if exact else "text:substr"
        return self.locators.get((key, name),
                                 _FakeLocator(exists=False))

    def locator(self, sel):
        return self.locators.get(("css", sel),
                                 _FakeLocator(exists=False))


class _FakeContext:
    def __init__(self):
        self.pages: list[_FakePage] = []
        self._closed = False
        self.browser = None   # not needed by _ctx_alive branch we hit

    def is_closed(self):
        return self._closed

    async def new_page(self):
        if self._closed:
            raise RuntimeError("Target page, context or browser has "
                               "been closed")
        p = _FakePage()
        self.pages.append(p)
        return p


# ── Helper to build a session without running async init ───────────────

def _build_session() -> bc._BrowserSession:
    sess = bc._BrowserSession.__new__(bc._BrowserSession)
    sess.browser_name = "fake"
    sess._context = _FakeContext()
    sess._page = None
    sess._loop = None
    sess._cdp_browser = None
    sess._pw = None
    return sess


# ── Tests ──────────────────────────────────────────────────────────────

def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_ctx_alive_detects_closed_context():
    sess = _build_session()
    assert sess._ctx_alive() is True
    sess._context._closed = True
    assert sess._ctx_alive() is False


def test_first_live_page_skips_closed():
    sess = _build_session()
    a, b, c = _FakePage("a"), _FakePage("b"), _FakePage("c")
    a._closed = True
    sess._context.pages = [a, b, c]
    assert sess._first_live_page() is b


def test_close_tab_recovers_to_live_sibling():
    sess = _build_session()
    a, b = _FakePage("a"), _FakePage("b")
    sess._context.pages = [a, b]
    sess._page = a

    async def _run_close():
        return await sess.close_tab()

    # Before fix: sess._page would be assigned pages[-1] (b) OR a closed
    # page when b happened to also be closed. After fix: either a live
    # sibling or None, never a closed Page.
    _run(_run_close())
    assert sess._page is b
    assert not sess._page.is_closed()


def test_close_tab_cleans_stale_ref_without_crash():
    sess = _build_session()
    a, b = _FakePage("a"), _FakePage("b")
    a._closed = True   # stale
    sess._context.pages = [a, b]
    sess._page = a

    async def _call():
        return await sess.close_tab()
    msg = _run(_call())
    # Stale ref recovered — new _page is live.
    assert sess._page is b
    assert "recovered" in msg or "No active tab" in msg


def test_get_page_raises_rather_than_new_page_on_closed_context():
    sess = _build_session()
    sess._context._closed = True

    async def _call():
        return await sess._get_page()

    # Monkey-patch _launch to a no-op so we skip the real Playwright init.
    async def _noop():
        return None
    sess._launch = _noop   # type: ignore
    with pytest.raises(RuntimeError, match="BrowserContext has been closed"):
        _run(_call())


def test_click_tries_role_before_text():
    sess = _build_session()
    page = _FakePage()
    clicked = {"n": 0}

    def _on_click():
        clicked["n"] += 1

    page.locators[("role", "button:Sign Up")] = _FakeLocator(
        click_cb=_on_click)
    sess._context.pages = [page]
    sess._page = page

    async def _call():
        return await sess.click(text="Sign Up")

    async def _noop():
        return None
    sess._launch = _noop   # type: ignore
    msg = _run(_call())
    assert clicked["n"] == 1
    assert "role=button" in msg


def test_click_expands_synonyms_for_register_intent():
    """A page uses literal 'Sign Up' but the caller asked for 'register'.
    Synonym expansion from core/synonyms should bridge that."""
    sess = _build_session()
    page = _FakePage()
    hits = {"n": 0}

    def _on_click():
        hits["n"] += 1

    page.locators[("role", "button:Sign Up")] = _FakeLocator(
        click_cb=_on_click)
    sess._context.pages = [page]
    sess._page = page

    async def _noop():
        return None
    sess._launch = _noop   # type: ignore

    msg = _run(sess.click(text="register"))
    assert hits["n"] == 1
    assert "Sign Up" in msg and "expanded from" in msg


def test_click_bengali_register_label():
    sess = _build_session()
    page = _FakePage()
    hits = {"n": 0}
    page.locators[("role", "button:একাউন্ট তৈরি করুন")] = _FakeLocator(
        click_cb=lambda: hits.__setitem__("n", hits["n"] + 1))
    sess._context.pages = [page]
    sess._page = page

    async def _noop():
        return None
    sess._launch = _noop   # type: ignore
    _run(sess.click(text="register"))
    assert hits["n"] == 1


def test_fill_form_verifies_values_with_readback():
    sess = _build_session()
    page = _FakePage()
    email_input = _FakeLocator(value="")
    name_input = _FakeLocator(value="")
    page.locators[("role", "textbox:Email")] = email_input
    page.locators[("label", "Email")] = email_input
    page.locators[("role", "textbox:Full Name")] = name_input
    page.locators[("label", "Full Name")] = name_input
    sess._context.pages = [page]
    sess._page = page

    async def _noop():
        return None
    sess._launch = _noop   # type: ignore
    msg = _run(sess.fill_form({"email": "ada@example.com",
                                "full_name": "Ada L"}))
    # Both values must round-trip via input_value() to count as verified.
    assert "2/2 verified" in msg
    assert "ada@example.com" in msg
    assert "Ada L" in msg


def test_fill_form_reports_mismatch_when_readback_differs():
    sess = _build_session()
    page = _FakePage()

    class _SilentlyDroppingInput(_FakeLocator):
        async def fill(self, v):
            # Simulates a React input that resets on blur — accepts the
            # typed value but clears it before the next read.
            self._value = ""
    bad = _SilentlyDroppingInput(value="", input_type="email")
    page.locators[("role", "textbox:Email")] = bad
    sess._context.pages = [page]
    sess._page = page

    async def _noop():
        return None
    sess._launch = _noop   # type: ignore
    msg = _run(sess.fill_form({"email": "x@y.z"}))
    assert "0/1 verified" in msg
    assert "Mismatch" in msg


def test_fill_form_masks_password_in_summary():
    sess = _build_session()
    page = _FakePage()
    pwd = _FakeLocator(value="", input_type="password")
    page.locators[("role", "textbox:password")] = pwd
    page.locators[("label", "password")] = pwd
    sess._context.pages = [page]
    sess._page = page

    async def _noop():
        return None
    sess._launch = _noop   # type: ignore
    msg = _run(sess.fill_form({"password": "SuperSecret42"}))
    assert "SuperSecret42" not in msg
    assert "(masked)" in msg


def test_synonyms_english_and_bengali_register():
    forms = synonyms.expand("register")
    assert "Sign Up" in forms
    assert "Create Account" in forms
    assert any("একাউন্ট" in f for f in forms)


def test_synonyms_intent_reverse_lookup():
    assert synonyms.intent_for("Please enter your mobile number") == "phone"
    assert synonyms.intent_for("ইমেইল ঠিকানা") == "email"
    assert synonyms.intent_for("Create Account") in ("register", "submit")
