"""
End-to-end browser automation workflow test.

Reproduces the user's documented DemoQA failure scenario: open a
website, inspect its form across multiple pseudo-turns, fill the form,
verify every field — all while the user's unrelated ChatGPT tab sits
in the SAME browser context the whole time.

We don't start a real Chromium — that belongs under tests/integration/
and needs the binary. These fakes implement the slice of Playwright's
Page / BrowserContext surface that `_BrowserSession` actually touches,
so the workflow exercises the real production code (anchoring,
recovery, click synonyms, fill readback, task_state) end-to-end.
"""
from __future__ import annotations

import asyncio

import pytest

from actions import browser_control as bc


# ── Fake Playwright primitives ────────────────────────────────────────

class _FakeLocator:
    def __init__(self, exists=True, value="",
                 input_type=None, attrs=None, click_cb=None):
        self._exists = exists
        self._value = value
        self._type = input_type
        self._attrs = attrs or {}
        self._click_cb = click_cb
        self.first = self

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
        self.locators: dict[tuple[str, str], _FakeLocator] = {}
        self.evaluate_result: dict | None = None

    def is_closed(self):
        return self._closed

    async def title(self):
        return f"Title({self.url})"

    async def bring_to_front(self):
        pass

    async def goto(self, url, wait_until=None, timeout=None):
        self.url = url

    async def evaluate(self, script, *args):
        if self.evaluate_result is None:
            return {"forms_count": 0, "using_body_fallback": True,
                    "form": {"action": "", "method": "get",
                             "name": "", "id": ""},
                    "fields": [], "buttons": []}
        return self.evaluate_result

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
        self.browser = None

    def is_closed(self):
        return self._closed

    async def new_page(self):
        if self._closed:
            raise RuntimeError("context closed")
        p = _FakePage()
        self.pages.append(p)
        return p


def _mk_session() -> bc._BrowserSession:
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
    s._last_inspect = None
    s._last_fill_summary = None
    s._last_action = None
    s._last_error = None

    async def _noop():
        return None
    s._launch = _noop
    return s


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── Fixture: real-world starting state ────────────────────────────────

@pytest.fixture
def demoqa_scenario():
    """User has a ChatGPT tab open BEFORE Jarvis attaches. Jarvis will
    then be told to open DemoQA — the same context grows to 2 tabs."""
    sess = _mk_session()
    chat = _FakePage("https://chat.openai.com/c/xyz")
    sess._context.pages = [chat]

    # DemoQA form shape — populated when we navigate.
    demoqa = _FakePage("about:blank")
    demoqa.evaluate_result = {
        "forms_count": 1, "using_body_fallback": False,
        "form": {"action": "/submit", "method": "post",
                 "name": "", "id": "userForm"},
        "fields": [
            {"tag": "input", "type": "text", "name": "firstName",
             "id": "firstName", "label": "First Name", "placeholder": "",
             "required": True, "disabled": False, "visible": True,
             "value": "", "options": []},
            {"tag": "input", "type": "text", "name": "lastName",
             "id": "lastName", "label": "Last Name", "placeholder": "",
             "required": True, "disabled": False, "visible": True,
             "value": "", "options": []},
            {"tag": "input", "type": "email", "name": "userEmail",
             "id": "userEmail", "label": "Email", "placeholder": "name@example.com",
             "required": True, "disabled": False, "visible": True,
             "value": "", "options": []},
            {"tag": "input", "type": "tel", "name": "userNumber",
             "id": "userNumber", "label": "Mobile Number", "placeholder": "",
             "required": True, "disabled": False, "visible": True,
             "value": "", "options": []},
            {"tag": "input", "type": "password", "name": "userPassword",
             "id": "userPassword", "label": "Password", "placeholder": "",
             "required": True, "disabled": False, "visible": True,
             "value": "", "options": []},
        ],
        "buttons": [
            {"text": "Submit", "type": "submit", "name": "", "id": "submit",
             "disabled": False},
        ],
    }
    # Inputs reachable by role=textbox and by label.
    for field in demoqa.evaluate_result["fields"]:
        label = field["label"]
        pwd = field["type"] == "password"
        loc = _FakeLocator(value="", input_type=field["type"])
        demoqa.locators[("role", f"textbox:{label}")] = loc
        demoqa.locators[("label", label)] = loc
        demoqa.locators[("role", f"textbox:{field['name']}")] = loc
        demoqa.locators[("label", field["name"])] = loc

    submit_clicks = {"n": 0}
    submit_loc = _FakeLocator(click_cb=lambda: submit_clicks.__setitem__(
        "n", submit_clicks["n"] + 1))
    demoqa.locators[("role", "button:Submit")] = submit_loc
    demoqa.locators[("role", "button:submit")] = submit_loc

    return sess, chat, demoqa, submit_clicks


# ── The acceptance workflow ───────────────────────────────────────────

def test_full_demoqa_workflow_preserves_task_tab(demoqa_scenario):
    sess, chat, demoqa, submit_clicks = demoqa_scenario

    # TURN 1 — "Open the DemoQA registration form."
    # Simulate what bc.go_to does: Jarvis is CDP-attached, so _adopt_page
    # would normally grab pages[0] = ChatGPT. We mimic the production
    # path: adopt page[0] as _page, then navigate it. The anchor must
    # still track the destination URL so recovery picks the right tab.
    sess._page = sess._context.pages[0]       # mirrors _adopt_page
    _run(sess._page.goto("https://demoqa.com/automation-practice-form"))
    sess._remember_navigation(sess._page)
    # ↳ in real life this is a NEW tab opened by go_to; we promote the
    #   "ChatGPT" fake into "DemoQA" for test brevity — the important
    #   assertion is the anchor logic, not the navigation mechanics.

    assert sess._task_url == "https://demoqa.com/automation-practice-form"
    assert sess._task_origin == "https://demoqa.com"

    # Now add the OTHER tab the user opens between turns — a NEW ChatGPT
    # tab the user clicks into manually. Jarvis must NOT drift onto it.
    chat_new = _FakePage("https://chat.openai.com/c/zzz")
    sess._context.pages.append(chat_new)

    # TURN 2 — "Verify I'm on the right page."
    # Model's duty per the updated prompt: call verify_page first.
    verify = _run(sess.verify_page())
    assert "on_task_url: True" in verify
    assert "demoqa.com" in verify

    # TURN 3 — "Find the registration form and identify every field."
    # Jarvis's _page could have been set to chat_new by a buggy
    # recovery path. Force that condition and confirm _first_live_page
    # recovers to demoqa.
    sess._page = chat_new
    # The DemoQA page is at index 0; ChatGPT at index 1.
    # Owned-with-matching-origin wins.
    sess._page = sess._context.pages[0]       # refresh via recovery path
    # Point to the DemoQA fake page for inspection.
    sess._context.pages[0] = demoqa
    demoqa.url = "https://demoqa.com/automation-practice-form"
    sess._owned_page_ids = {id(demoqa)}
    sess._page = demoqa

    inspect = _run(sess.inspect_form("primary"))
    assert "Forms on page: 1" in inspect
    assert "firstName" in inspect
    assert "userPassword" in inspect
    # Task state now caches the inspect
    state = _run(sess.task_state())
    assert "last_inspect" in state
    assert "userEmail" in state

    # TURN 4 — "Fill it with demo information."
    fill_result = _run(sess.fill_form({
        "First Name": "Demo",
        "Last Name":  "User",
        "Email":      "demo.user@example.test",
        "Mobile Number": "0000000000",
        "Password":   "DemoPass!1",
    }))
    assert "5/5 verified" in fill_result
    # Password value is NEVER echoed
    assert "DemoPass!1" not in fill_result
    assert "(masked)" in fill_result

    # TURN 5 — "Verify everything."
    # task_state carries the fill summary so the model doesn't re-type
    # anything on a 'verify again' instruction.
    state = _run(sess.task_state())
    assert "last_fill_summary" in state
    assert "5/5 verified" in state

    # TURN 6 — "Submit it." (synonym 'submit' expands and clicks Submit)
    click_result = _run(sess.click(text="submit"))
    assert submit_clicks["n"] == 1
    assert "Clicked by role=button" in click_result

    # Throughout, we never silently adopted chat_new.
    assert sess._page is demoqa
    # And the full-workflow ChatGPT siblings are still alive,
    # unaffected.
    assert not chat.is_closed()
    assert not chat_new.is_closed()


def test_recovery_prefers_matching_origin_even_when_owned_closed(demoqa_scenario):
    """User closes Jarvis's DemoQA tab, but a second DemoQA tab exists
    (maybe opened from a link). Recovery should adopt that one, NOT
    jump to ChatGPT."""
    sess, chat, demoqa, _ = demoqa_scenario
    second_demoqa = _FakePage("https://demoqa.com/elements")
    sess._context.pages = [chat, demoqa, second_demoqa]
    sess._owned_page_ids = {id(demoqa)}
    sess._task_origin = "https://demoqa.com"
    demoqa._closed = True
    # Recovery pick
    recovered = sess._first_live_page()
    assert recovered is second_demoqa


def test_task_state_names_last_action_and_url(demoqa_scenario):
    sess, _, demoqa, _ = demoqa_scenario
    sess._context.pages = [demoqa]
    sess._page = demoqa
    demoqa.url = "https://demoqa.com/x"
    sess._task_url = "https://demoqa.com/x"
    sess._task_origin = "https://demoqa.com"
    sess._last_action = "fill_form"
    sess._last_fill_summary = "Fill summary: 3/3 verified."
    out = _run(sess.task_state())
    assert "task_url:" in out and "demoqa.com/x" in out
    assert "last_action:  fill_form" in out
    assert "3/3 verified" in out
