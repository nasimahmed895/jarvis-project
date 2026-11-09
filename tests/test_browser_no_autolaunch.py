"""
Regression tests for the "new browser opens on inspect" bug.

Previously, when the user asked Jarvis to inspect / verify / dismiss /
fill without an active Playwright session, the dispatcher fell through
to `_registry.get(browser)` which called `launch_persistent_context` —
opening a fresh automation window on a tab the user could not see.

After the fix, DOM-only verbs must return a clear "needs attach /
start_debug" error instead of silently launching.

We drive `browser_control(...)` directly and monkey-patch the dispatcher
dependencies so no real browser is ever started.
"""
from __future__ import annotations

import asyncio

import pytest

from actions import browser_control as bc


@pytest.fixture(autouse=True)
def _wipe_registry(monkeypatch):
    # Reset the global _SessionRegistry so no prior test's state leaks.
    monkeypatch.setattr(bc._registry, "_sessions", {}, raising=False)
    monkeypatch.setattr(bc._registry, "_active_browser", None,
                        raising=False)
    # Pretend AppleScript bridge has nothing to say for these verbs.
    monkeypatch.setattr(bc, "_try_applescript",
                        lambda *a, **kw: None)
    # Pretend no running native browsers so the auto-attach path exits
    # cleanly rather than trying CDP.
    monkeypatch.setattr(bc, "_detect_running_browsers", lambda: set())
    yield


def _run(action, **extra):
    return bc.browser_control(parameters={"action": action,
                                           "browser": "chrome",
                                           **extra})


# Each DOM-only verb must refuse to auto-launch.

@pytest.mark.parametrize("verb", [
    "inspect_form", "verify_page", "task_state",
    "dismiss_dialogs", "fill_form",
    "smart_click", "smart_type",
])
def test_dom_verb_refuses_autolaunch(verb):
    extra = {}
    if verb == "fill_form":
        extra["fields"] = {"email": "x@y.z"}
    out = _run(verb, **extra)
    assert "Cannot run" in out
    assert verb in out
    # Instruction surfaces what the user should do next.
    assert ("attach" in out or "start_debug" in out)
    # Explicit do-not-open clause tells the user we won't silently
    # launch an invisible window.
    assert ("separate automation window" in out
            or "cannot see" in out)


def test_fill_form_failed_prefix_on_zero_verified(monkeypatch):
    """`fill_form` summary must front-load FAILED when 0/N round-trip."""
    class _FakeLocator:
        def __init__(self):
            self._value = ""
            self.first = self

        async def count(self):
            return 0  # locator never matches — simulates missing input

        async def wait_for(self, state="visible", timeout=4_000):
            return None

        async def fill(self, v):
            pass

        async def input_value(self):
            return ""

        async def get_attribute(self, name):
            return None

    class _FakePage:
        def is_closed(self):
            return False

        def get_by_role(self, *a, **kw):
            return _FakeLocator()

        def get_by_label(self, *a, **kw):
            return _FakeLocator()

        def get_by_placeholder(self, *a, **kw):
            return _FakeLocator()

        def locator(self, *a, **kw):
            return _FakeLocator()

    sess = bc._BrowserSession.__new__(bc._BrowserSession)
    sess.browser_name = "fake"
    sess._context = None
    sess._page = _FakePage()
    sess._task_url = None
    sess._task_origin = None
    sess._owned_page_ids = set()
    sess._last_inspect = None
    sess._last_fill_summary = None
    sess._last_action = None
    sess._last_error = None

    async def _noop():
        return None
    sess._launch = _noop

    out = asyncio.new_event_loop().run_until_complete(
        sess.fill_form({"firstName": "Ada", "email": "ada@example.test"}))
    assert out.startswith("FAILED:")
    assert "NOT filled" in out or "not filled" in out.lower()
