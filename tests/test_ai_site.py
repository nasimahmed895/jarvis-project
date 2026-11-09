"""
Tests for core/ai_adapters and actions/ai_site.

We stub `browser_control.browser_control` end-to-end so the suite does
NOT start Chromium. Each stub call is scripted to return whatever the
adapter path expects at that step, letting us assert:

  * `open` reuses the active tab via verify_page when hosts match.
  * `ask` calls fill_form with the adapter's composer labels and clicks
    one of the adapter's send labels.
  * On FAILED fill summary, `ask` returns a diagnostic — never pretends.
  * `orchestrate` chains steps, substitutes {{prev_response}}, and
    short-circuits on failure.
  * `list_sites` enumerates the registry.
"""
from __future__ import annotations

import json

import pytest

from actions import ai_site
from core import ai_adapters


# ── Adapter registry ─────────────────────────────────────────────────

def test_registry_contains_three_sites():
    keys = set(ai_adapters.available())
    assert {"chatgpt", "claude", "gemini"}.issubset(keys)


def test_aliases_resolve():
    assert ai_adapters.get("gpt").key == "chatgpt"
    assert ai_adapters.get("bard").key == "gemini"
    assert ai_adapters.get("anthropic").key == "claude"
    assert ai_adapters.get("Unknown") is None


def test_detect_from_url():
    assert ai_adapters.detect_from_url(
        "https://chat.openai.com/c/x").key == "chatgpt"
    assert ai_adapters.detect_from_url(
        "https://claude.ai/chat/abc").key == "claude"
    assert ai_adapters.detect_from_url(
        "https://gemini.google.com/app").key == "gemini"
    assert ai_adapters.detect_from_url(
        "https://example.com/") is None


def test_register_adds_new_adapter():
    """Plugins can add new sites without changing shipping code."""
    extra = ai_adapters.Adapter(
        key="perplexity", name="Perplexity",
        url="https://www.perplexity.ai/",
        url_hosts=("perplexity.ai",),
        composer=("Ask anything",),
        send_labels=("Submit",),
        response_selector=(".prose:last-of-type",),
        busy_selector=(".typing-indicator",),
    )
    try:
        ai_adapters.register(extra)
        assert ai_adapters.get("perplexity") is extra
        assert ai_adapters.detect_from_url(
            "https://www.perplexity.ai/search/x").key == "perplexity"
    finally:
        ai_adapters._ADAPTERS.pop("perplexity", None)


# ── ai_site action: fake browser_control ─────────────────────────────

class _FakeBC:
    """Scripted responses per (action, important_param_tuple)."""
    def __init__(self, script: dict):
        self.script = dict(script)
        self.calls: list[dict] = []
        self._fallback = {"verify_page": "page verify:\n  url: about:blank\n  on_task_url: False",
                          "go_to": "Opened: ok",
                          "get_text": "stubbed body text",
                          "click": "Clicked by text(substr) 'x'",
                          "fill_form": "Fill summary: 1/1 verified."}

    def browser_control(self, parameters=None, **kw):
        p = parameters or {}
        self.calls.append(p)
        # Look up by action + first structural arg.
        action = p.get("action")
        key = (action,
               p.get("url") or p.get("text") or p.get("prompt") or "")
        if key in self.script:
            return self.script[key]
        if (action,) in self.script:
            return self.script[(action,)]
        # Action-only lookup.
        if action in self.script:
            return self.script[action]
        return self._fallback.get(action, "ok")


@pytest.fixture
def fake_bc(monkeypatch):
    def _install(script):
        bc = _FakeBC(script)
        monkeypatch.setattr(ai_site, "_bc",
                            type("_", (), {"browser_control":
                                           staticmethod(bc.browser_control)}))
        return bc
    return _install


# Keep real time.sleep from slowing tests — ai_site polls at 1s.
@pytest.fixture(autouse=True)
def _fast_sleep(monkeypatch):
    monkeypatch.setattr("actions.ai_site.time.sleep", lambda s: None)


def test_list_sites_enumerates_registry():
    out = ai_site.ai_site({"action": "list_sites"})
    assert "ChatGPT" in out
    assert "Claude" in out
    assert "Gemini" in out


def test_open_navigates_when_not_on_site(fake_bc):
    bc = fake_bc({
        "verify_page": "page verify:\n  url: about:blank\n  on_task_url: False",
        "go_to":       "Opened: https://claude.ai/new",
    })
    out = ai_site.ai_site({"action": "open", "site": "claude"})
    assert "Opened" in out
    # go_to was called with the adapter's canonical URL.
    nav = [c for c in bc.calls if c.get("action") == "go_to"]
    assert nav and nav[0]["url"] == "https://claude.ai/new"


def test_open_reuses_tab_when_already_on_site(fake_bc):
    bc = fake_bc({
        "verify_page": ("page verify:\n  url: https://claude.ai/chat/abc\n"
                        "  on_task_url: True\n  host: claude.ai"),
    })
    out = ai_site.ai_site({"action": "open", "site": "claude"})
    assert "Already on Claude" in out
    # No go_to fired.
    assert not any(c.get("action") == "go_to" for c in bc.calls)


def test_ask_fails_fast_when_composer_not_found(fake_bc):
    bc = fake_bc({
        "verify_page": ("page verify:\n  url: https://claude.ai/\n"
                        "  on_task_url: True\n  host: claude.ai"),
        "fill_form":   ("FAILED: Fill summary: 0/5 verified. No "
                        "field locator matched; the form was NOT "
                        "filled."),
    })
    out = ai_site.ai_site({"action": "ask", "site": "claude",
                           "prompt": "hi"})
    assert out.startswith("FAILED")
    assert "Composer not found" in out
    # Never clicked send when the fill failed.
    assert not any(c.get("action") == "click" for c in bc.calls)


def test_ask_sends_when_fill_verified(fake_bc):
    bc = fake_bc({
        "verify_page": ("page verify:\n  url: https://claude.ai/\n"
                        "  on_task_url: True\n  host: claude.ai"),
        "fill_form":   "Fill summary: 1/1 verified.",
        "click":       "Clicked by role=button name='Send Message'",
        "get_text":    "Hello, this is Claude's answer.",
    })
    out = ai_site.ai_site({"action": "ask", "site": "claude",
                           "prompt": "hi"})
    assert out.startswith("[Claude] response:")
    assert "Claude's answer" in out


def test_follow_up_runs_without_navigation(fake_bc):
    bc = fake_bc({
        "verify_page": ("page verify:\n  url: https://claude.ai/\n"
                        "  on_task_url: True\n  host: claude.ai"),
        "fill_form":   "Fill summary: 1/1 verified.",
        "click":       "Clicked by role=button",
        "get_text":    "Follow-up answer text.",
    })
    out = ai_site.ai_site({"action": "follow_up", "site": "claude",
                           "prompt": "more?"})
    assert out.startswith("[Claude] follow-up:")


def test_new_chat_calls_adapter_label(fake_bc):
    bc = fake_bc({
        "click": "Clicked by role=link name='New chat'",
    })
    out = ai_site.ai_site({"action": "new_chat", "site": "chatgpt"})
    assert "Started a new ChatGPT conversation" in out


def test_orchestrate_substitutes_prev_response(fake_bc):
    """Two-site workflow: Claude answers, ChatGPT receives Claude's
    text injected via {{prev_response}}."""
    class _BC:
        def __init__(self):
            self.calls: list[dict] = []
            self._fills = 0           # 0 before any ask; 1 after first; 2 after second

        def browser_control(self, parameters=None, **kw):
            self.calls.append(parameters)
            action = parameters.get("action")
            if action == "verify_page":
                return "page verify:\n  url: about:blank\n  on_task_url: False"
            if action == "go_to":
                return "Opened."
            if action == "fill_form":
                self._fills += 1
                return "Fill summary: 1/1 verified."
            if action == "click":
                return "Clicked by role=button"
            if action == "get_text":
                # Response produced depends on which ask we're inside.
                if self._fills <= 1:
                    return "Claude produced a business plan: SaaS for cats."
                return "ChatGPT improved writing: refined business plan."
            return "ok"

    stub = _BC()
    import actions.ai_site as mod
    mod._bc = type("_", (), {"browser_control":
                             staticmethod(stub.browser_control)})

    out = ai_site.ai_site({
        "action": "orchestrate",
        "steps": [
            {"site": "claude",
             "prompt": "Draft a business plan"},
            {"site": "chatgpt",
             "prompt": "Improve: {{prev_response}}"},
        ],
    })
    data = json.loads(out)
    assert len(data["steps"]) == 2
    assert all(s["ok"] for s in data["steps"])
    # The second step's prompt interpolated the first response.
    # We inspect the fill_form call for step 2's prompt.
    fills = [c for c in stub.calls if c.get("action") == "fill_form"]
    assert len(fills) == 2
    s2_values = list(fills[1].get("fields", {}).values())
    assert any("Claude produced" in v for v in s2_values)


def test_orchestrate_short_circuits_on_failure(fake_bc):
    class _BC:
        def __init__(self):
            self.calls: list[dict] = []

        def browser_control(self, parameters=None, **kw):
            self.calls.append(parameters)
            action = parameters.get("action")
            if action == "verify_page":
                return "page verify:\n  on_task_url: False"
            if action == "go_to":
                return "Opened."
            if action == "fill_form":
                return ("FAILED: Fill summary: 0/5 verified. No field "
                        "locator matched; the form was NOT filled.")
            if action == "click":
                return "no"
            if action == "get_text":
                return ""
            return "ok"

    stub = _BC()
    import actions.ai_site as mod
    mod._bc = type("_", (), {"browser_control":
                             staticmethod(stub.browser_control)})

    out = ai_site.ai_site({
        "action": "orchestrate",
        "steps": [
            {"site": "claude",  "prompt": "a"},
            {"site": "chatgpt", "prompt": "b"},
        ],
    })
    data = json.loads(out)
    # Only one step attempted — second never runs.
    assert len(data["steps"]) == 1
    assert data["steps"][0]["ok"] is False
    assert "error" in data["steps"][0]
