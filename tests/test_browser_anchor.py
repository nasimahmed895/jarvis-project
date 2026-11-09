"""
Tests for the task-URL anchoring that prevents cross-tab leaks.

Reproduces the exact scenario from the user's logs: user has ChatGPT
open, Jarvis is told to open DemoQA on the SAME browser context, then
the task page is lost — before the fix `_first_live_page()` would pick
the ChatGPT tab back up. With the fix in place it must prefer the owned
tab, and when all owned tabs are gone it must prefer the one whose
origin still matches the task URL.
"""
from __future__ import annotations

import asyncio

import pytest

from actions import browser_control as bc


class _FakePage:
    def __init__(self, url="about:blank"):
        self.url = url
        self._closed = False

    def is_closed(self):
        return self._closed

    async def title(self):
        return f"Title({self.url})"

    async def bring_to_front(self):
        pass

    async def goto(self, url, wait_until=None, timeout=None):
        self.url = url

    async def evaluate(self, script, *args):
        # stub — not exercised in this file
        return {}


class _FakeContext:
    def __init__(self):
        self.pages: list[_FakePage] = []
        self._closed = False
        self.browser = None

    def is_closed(self):
        return self._closed


def _mk_session():
    s = bc._BrowserSession.__new__(bc._BrowserSession)
    s.browser_name = "fake"
    s._context = _FakeContext()
    s._page = None
    s._loop = None
    s._cdp_browser = None
    s._pw = None
    s._task_url = None
    s._task_origin = None
    s._owned_page_ids = set()
    return s


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── Origin helper ─────────────────────────────────────────────────────

def test_origin_of_strips_path_and_query():
    assert bc._BrowserSession._origin_of(
        "https://demoqa.com/automation-practice-form?a=1"
    ) == "https://demoqa.com"
    assert bc._BrowserSession._origin_of("about:blank") is None
    assert bc._BrowserSession._origin_of(None) is None
    assert bc._BrowserSession._origin_of("") is None


# ── first_live_page preference order ──────────────────────────────────

def test_prefers_owned_with_matching_origin():
    sess = _mk_session()
    chat = _FakePage("https://chat.openai.com/c/abc")      # user's tab
    demo = _FakePage("https://demoqa.com/forms")           # Jarvis's tab
    other_demo = _FakePage("https://demoqa.com/elements")  # another demo tab,
                                                           # NOT owned
    sess._context.pages = [chat, demo, other_demo]
    sess._owned_page_ids = {id(demo)}
    sess._task_url = "https://demoqa.com/automation-practice-form"
    sess._task_origin = "https://demoqa.com"
    assert sess._first_live_page() is demo


def test_prefers_matching_origin_when_no_owned_live():
    sess = _mk_session()
    chat = _FakePage("https://chat.openai.com/c/abc")
    demo = _FakePage("https://demoqa.com/forms")
    sess._context.pages = [chat, demo]
    # Jarvis's original tab closed; we have no owned ids.
    sess._task_origin = "https://demoqa.com"
    assert sess._first_live_page() is demo


def test_falls_back_to_first_live_when_no_signal():
    sess = _mk_session()
    chat = _FakePage("https://chat.openai.com/c/abc")
    demo = _FakePage("https://demoqa.com/forms")
    sess._context.pages = [chat, demo]
    # No task URL set yet (brand-new session)
    assert sess._first_live_page() is chat


def test_closed_task_tab_recovers_to_matching_sibling():
    sess = _mk_session()
    chat = _FakePage("https://chat.openai.com/c/abc")
    demo1 = _FakePage("https://demoqa.com/forms")
    demo2 = _FakePage("https://demoqa.com/elements")
    demo1._closed = True   # Jarvis's original tab was closed
    sess._context.pages = [chat, demo1, demo2]
    sess._owned_page_ids = {id(demo1)}
    sess._task_origin = "https://demoqa.com"
    # Owned but closed → skip. Match origin → demo2.
    assert sess._first_live_page() is demo2


def test_remember_navigation_sets_task_url_and_origin():
    sess = _mk_session()
    p = _FakePage("https://demoqa.com/automation-practice-form")
    sess._remember_navigation(p)
    assert sess._task_url == "https://demoqa.com/automation-practice-form"
    assert sess._task_origin == "https://demoqa.com"
    assert id(p) in sess._owned_page_ids


def test_remember_navigation_ignores_blank():
    sess = _mk_session()
    p = _FakePage("about:blank")
    sess._remember_navigation(p)
    # task URL stays None — blank is not a destination to anchor on.
    assert sess._task_url is None
    assert sess._task_origin is None
    # But we still mark the page object as owned, so a later navigation
    # on the same page object is recognised as a Jarvis-owned tab.
    assert id(p) in sess._owned_page_ids


# ── verify_page output ────────────────────────────────────────────────

def test_verify_page_reports_on_task_true_and_false():
    sess = _mk_session()
    demo = _FakePage("https://demoqa.com/forms")
    sess._context.pages = [demo]
    sess._page = demo
    sess._owned_page_ids = {id(demo)}
    sess._task_url = "https://demoqa.com/automation-practice-form"
    sess._task_origin = "https://demoqa.com"

    async def _noop():
        return None
    sess._launch = _noop

    msg = _run(sess.verify_page())
    assert "on_task_url: True" in msg
    assert "demoqa.com" in msg
    assert "owned: True" in msg

    # Simulate a surprise navigation off-task
    demo.url = "https://chat.openai.com/c/abc"
    msg = _run(sess.verify_page())
    assert "on_task_url: False" in msg
