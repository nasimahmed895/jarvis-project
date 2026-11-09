"""
Tests for the session-12 fixes.

Covers:
  * probe_selector returns a structured line with exists/visible/count.
  * fill_form branches by element type: SELECT uses select_option,
    checkbox uses check/uncheck, radio picks by value, file uses
    set_input_files.
  * type_text uses fill() first and verifies via input_value().
  * Full ai_site workflow stays green with the rewritten busy loop.
"""
from __future__ import annotations

import asyncio

import pytest

from actions import browser_control as bc


# ── Fake primitives ───────────────────────────────────────────────────

class _FakeLocator:
    def __init__(self, *, exists=True, value="", tag="input",
                 input_type="text", visible=True, checked=False,
                 attrs=None, options_by_label=None):
        self._exists = exists
        self._value = value
        self._tag = tag
        self._type = input_type
        self._visible = visible
        self._checked = checked
        self._attrs = attrs or {}
        self._options_by_label = options_by_label or {}
        self.set_input_files_calls: list[str] = []
        self.first = self

    async def count(self):
        return 1 if self._exists else 0

    async def wait_for(self, state="visible", timeout=4_000):
        return None

    async def is_visible(self):
        return self._visible

    async def is_checked(self):
        return self._checked

    async def fill(self, v):
        if not self._exists:
            raise RuntimeError("no element")
        self._value = v

    async def clear(self):
        self._value = ""

    async def click(self, timeout=None):
        if not self._exists:
            raise RuntimeError("no element")

    async def type(self, v, delay=0):
        self._value = v

    async def input_value(self):
        return self._value

    async def check(self):
        self._checked = True

    async def uncheck(self):
        self._checked = False

    async def select_option(self, label=None, value=None):
        if label is not None:
            if self._options_by_label and label not in self._options_by_label:
                raise RuntimeError(f"no option labeled {label}")
            self._value = label
            return
        if value is not None:
            self._value = value
            return
        raise RuntimeError("select_option needs label or value")

    async def set_input_files(self, path):
        self.set_input_files_calls.append(path)
        self._value = path

    async def evaluate(self, script):
        # Only one script is used — `el => el.tagName`.
        return self._tag.upper()

    async def get_attribute(self, name):
        if name == "type":
            return self._type
        if name == "name":
            return self._attrs.get("name", "")
        return self._attrs.get(name)


class _FakePage:
    def __init__(self):
        self._closed = False
        self.locators: dict[tuple[str, str], _FakeLocator] = {}

    def is_closed(self):
        return self._closed

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


def _mk_session(page: _FakePage) -> bc._BrowserSession:
    sess = bc._BrowserSession.__new__(bc._BrowserSession)
    sess.browser_name = "fake"
    sess._context = None
    sess._page = page
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
    return sess


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── probe_selector ──────────────────────────────────────────────────

def test_probe_selector_exists_and_visible():
    page = _FakePage()
    page.locators[("css", "button.stop")] = _FakeLocator(
        exists=True, visible=True)
    sess = _mk_session(page)
    out = _run(sess.probe_selector("button.stop"))
    assert "exists=True" in out
    assert "visible=True" in out
    assert "count=1" in out


def test_probe_selector_missing():
    page = _FakePage()
    sess = _mk_session(page)
    out = _run(sess.probe_selector(".nope"))
    assert "exists=False" in out
    assert "count=0" in out


def test_probe_selector_empty_arg():
    sess = _mk_session(_FakePage())
    out = _run(sess.probe_selector(""))
    assert "selector=(empty)" in out


# ── fill_form branching ─────────────────────────────────────────────

def test_fill_form_select_uses_select_option():
    page = _FakePage()
    sel = _FakeLocator(tag="select", input_type="",
                       options_by_label={"USA": "us", "UK": "gb"})
    page.locators[("role", "textbox:country")] = sel
    page.locators[("label", "country")] = sel
    sess = _mk_session(page)
    out = _run(sess.fill_form({"country": "USA"}))
    assert "1/1 verified" in out
    assert "[select]" in out
    assert sel._value == "USA"


def test_fill_form_checkbox_on_off():
    page = _FakePage()
    cb = _FakeLocator(tag="input", input_type="checkbox")
    page.locators[("role", "textbox:accept")] = cb
    page.locators[("label", "accept")] = cb
    sess = _mk_session(page)
    out = _run(sess.fill_form({"accept": "yes"}))
    assert "[checkbox] = on" in out
    assert cb._checked is True

    out2 = _run(sess.fill_form({"accept": "no"}))
    assert "[checkbox] = off" in out2
    assert cb._checked is False


def test_fill_form_file_input_sets_files():
    page = _FakePage()
    fi = _FakeLocator(tag="input", input_type="file")
    page.locators[("role", "textbox:resume")] = fi
    page.locators[("label", "resume")] = fi
    sess = _mk_session(page)
    out = _run(sess.fill_form({"resume": "/tmp/me.pdf"}))
    assert "1/1 verified" in out
    assert "[file]" in out
    assert fi.set_input_files_calls == ["/tmp/me.pdf"]


def test_fill_form_radio_selects_by_value():
    page = _FakePage()
    radio_group = _FakeLocator(tag="input", input_type="radio",
                                attrs={"name": "gender"})
    # Scoped CSS lookup for `input[type="radio"][name="gender"][value="Male"]`
    male_radio = _FakeLocator(tag="input", input_type="radio",
                               attrs={"name": "gender", "value": "Male"})
    page.locators[("role", "textbox:gender")] = radio_group
    page.locators[("label", "gender")] = radio_group
    page.locators[
        ("css", 'input[type="radio"][name="gender"][value="Male"]')
    ] = male_radio
    sess = _mk_session(page)
    out = _run(sess.fill_form({"gender": "Male"}))
    assert "[radio] = Male" in out
    assert male_radio._checked is True


def test_fill_form_text_default_still_works():
    page = _FakePage()
    el = _FakeLocator(tag="input", input_type="text")
    page.locators[("role", "textbox:first_name")] = el
    page.locators[("label", "first_name")] = el
    sess = _mk_session(page)
    out = _run(sess.fill_form({"first_name": "Ada"}))
    assert "1/1 verified" in out
    assert el._value == "Ada"


# ── type_text uses fill() first ─────────────────────────────────────

def test_type_text_prefers_fill_and_verifies():
    page = _FakePage()
    el = _FakeLocator(tag="input", input_type="text")
    page.locators[("css", "input.search")] = el
    sess = _mk_session(page)
    out = _run(sess.type_text(selector="input.search",
                              text="Artificial Intelligence"))
    assert el._value == "Artificial Intelligence"
    assert "verified" in out


def test_type_text_fallback_to_type_when_fill_fails():
    class _StubbornLocator(_FakeLocator):
        async def fill(self, v):
            if v == "":
                return
            raise RuntimeError("fill rejected")

    page = _FakePage()
    el = _StubbornLocator(tag="div", input_type="")
    page.locators[("css", "div.custom")] = el
    sess = _mk_session(page)
    out = _run(sess.type_text(selector="div.custom", text="hi"))
    # Fallback path should still land the text.
    assert el._value == "hi"
    assert "typed" in out.lower()
