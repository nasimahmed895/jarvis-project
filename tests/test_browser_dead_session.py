"""
Tests for the permanent-breakage bug.

Scenario: the user had Jarvis drive their real browser; Playwright opened
an automation-owned window; user closed that window; the cached session's
context became dead; every subsequent `_SessionRegistry.get()` returned
the SAME dead session, so every browser action replied
"BrowserContext has been closed" forever.

After the fix:
  * `_BrowserSession.is_dead()` recognises closed contexts and
    disconnected CDP browsers.
  * `_SessionRegistry._get_or_create()` evicts dead sessions and builds
    a fresh one.
  * The dispatcher catches "BrowserContext has been closed" errors and
    calls `_registry.evict(name)` so the next call starts clean.
"""
from __future__ import annotations

import pytest

from actions import browser_control as bc


class _FakeBrowser:
    def __init__(self, connected: bool = True):
        self._connected = connected

    def is_connected(self):
        return self._connected


class _FakeContext:
    def __init__(self, closed: bool = False, connected: bool = True):
        self._closed = closed
        self.browser = _FakeBrowser(connected=connected)

    def is_closed(self):
        return self._closed


def _mk_session(**attrs) -> bc._BrowserSession:
    s = bc._BrowserSession.__new__(bc._BrowserSession)
    s.browser_name = "chrome"
    s._context = attrs.get("ctx", _FakeContext(closed=False))
    s._page = None
    s._loop = None
    s._cdp_browser = attrs.get("cdp", None)
    s._pw = None
    s._task_url = None
    s._task_origin = None
    s._owned_page_ids = set()
    s._last_inspect = None
    s._last_fill_summary = None
    s._last_action = None
    s._last_error = None
    return s


# ── is_dead detection ────────────────────────────────────────────────

def test_is_dead_live_context():
    sess = _mk_session()
    assert sess.is_dead() is False


def test_is_dead_closed_context():
    sess = _mk_session(ctx=_FakeContext(closed=True))
    assert sess.is_dead() is True


def test_is_dead_disconnected_cdp():
    sess = _mk_session(cdp=_FakeBrowser(connected=False))
    assert sess.is_dead() is True


def test_is_dead_before_launch_is_not_dead():
    sess = _mk_session()
    sess._context = None
    assert sess.is_dead() is False


def test_is_dead_browser_disconnected_via_context():
    ctx = _FakeContext(closed=False, connected=False)
    sess = _mk_session(ctx=ctx)
    assert sess.is_dead() is True


# ── Registry evicts dead sessions ────────────────────────────────────

def test_registry_evicts_dead_session_on_get(monkeypatch):
    reg = bc._SessionRegistry()
    dead = _mk_session(ctx=_FakeContext(closed=True))
    # Short-circuit start() so the fresh replacement session doesn't
    # try to spin up a real Playwright thread in tests.

    class _NoStartSession(bc._BrowserSession):
        def start(self):
            # Pretend start succeeded.
            pass

    monkeypatch.setattr(bc, "_BrowserSession", _NoStartSession)
    reg._sessions["chrome"] = dead
    out = reg._get_or_create("chrome")
    assert out is not dead
    # The cache now holds the fresh one — not the dead one.
    assert reg._sessions["chrome"] is out


def test_registry_keeps_live_session(monkeypatch):
    reg = bc._SessionRegistry()
    live = _mk_session(ctx=_FakeContext(closed=False))

    class _NoStartSession(bc._BrowserSession):
        def start(self):
            pass

    monkeypatch.setattr(bc, "_BrowserSession", _NoStartSession)
    reg._sessions["chrome"] = live
    out = reg._get_or_create("chrome")
    assert out is live


def test_registry_evict_clears_active():
    reg = bc._SessionRegistry()
    reg._sessions["chrome"] = _mk_session()
    reg._active_browser = "chrome"
    reg.evict("chrome")
    assert "chrome" not in reg._sessions
    assert reg._active_browser == ""


# ── Dispatcher auto-evicts on terminal error ─────────────────────────

def test_dispatcher_auto_evicts_on_closed_context_error(monkeypatch):
    """When the Playwright action raises 'BrowserContext has been
    closed', the dispatcher must evict so the NEXT call starts fresh —
    the whole point of this bug fix."""
    evicted = []

    class _ReplacementRegistry(bc._SessionRegistry):
        pass

    reg = _ReplacementRegistry()

    class _FakeSession:
        def __init__(self, *a, **kw):
            self.browser_name = "chrome"

        def start(self):
            pass

        def is_dead(self):
            # First call: live (so registry returns us, not a replacement).
            # The action itself blows up with the closed-context error,
            # triggering the dispatcher's recovery path.
            return False

        def close(self):
            pass

        def run(self, coro, timeout: int = 60):
            # Simulate the exact closed-context error.
            raise RuntimeError(
                "BrowserContext.new_page: Target page, context or "
                "browser has been closed"
            )

        def go_to(self, url):
            return None

        def new_tab(self, url=""):
            return None

        def search(self, *a, **kw):
            return None

    # Pre-seed the registry with a session that will error.
    pre = _FakeSession()
    reg._sessions["chrome"] = pre
    # Prevent a real Playwright launch if the dispatcher's recovery
    # path tries to recreate the session.
    monkeypatch.setattr(bc, "_BrowserSession", _FakeSession)

    # Patch evict to record calls.
    orig_evict = reg.evict

    def _patched_evict(name):
        evicted.append(name)
        return orig_evict(name)
    reg.evict = _patched_evict   # type: ignore

    monkeypatch.setattr(bc, "_registry", reg)
    monkeypatch.setattr(bc, "_try_applescript", lambda *a, **kw: None)
    # No native browsers running — skip auto-attach logic.
    monkeypatch.setattr(bc, "_detect_running_browsers", lambda: set())
    # Grant consent so we don't get the permission gate.
    reg.grant("chrome")

    out = bc.browser_control(parameters={"action": "new_tab",
                                          "browser": "chrome"})
    # Error turned into a helpful message, not a raw traceback.
    assert "terminated" in out.lower() or "dead reference" in out.lower()
    assert "attach" in out.lower() or "re-run" in out.lower()
    # Eviction ran.
    assert "chrome" in evicted
