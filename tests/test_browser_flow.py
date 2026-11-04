"""Tests for actions/browser_flow.py.

Browser_control itself is driven in these tests through a mock so we
exercise the flow orchestration, CAPTCHA detection, and verifier logic
without launching a real Playwright session.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import actions.browser_flow as bf


# ── CAPTCHA detector ────────────────────────────────────────────────────────

def test_detect_captcha_url_pattern():
    r = bf.detect_captcha_markers(
        url="https://www.google.com/recaptcha/api2/anchor?ar=1",
    )
    assert r["detected"] is True
    assert r["confidence"] >= 0.5


def test_detect_captcha_text_heuristic():
    r = bf.detect_captcha_markers(text="Please verify you are human.")
    # Text alone → score 2 → detected
    assert r["detected"] is True


def test_detect_captcha_dom_only_is_likely_not_detected():
    r = bf.detect_captcha_markers(html="<div class='g-recaptcha'></div>")
    assert r["likely"] is True
    assert r["detected"] is False


def test_detect_captcha_requires_two_signals():
    url_sig = bf.detect_captcha_markers(
        url="https://challenges.cloudflare.com/challenge",
    )
    # URL alone scores 2 → detected
    assert url_sig["detected"] is True


def test_detect_captcha_clean_page():
    r = bf.detect_captcha_markers(
        url="https://example.com",
        text="Welcome to the page.",
        html="<html><body>Hello</body></html>",
    )
    assert r["detected"] is False
    assert r["likely"] is False


# ── Verifier ────────────────────────────────────────────────────────────────

def test_verify_url_contains_pass():
    r = bf.verify_navigation(
        current_url="https://mail.example.com/inbox",
        current_text="Inbox",
        expect_url_contains="/inbox",
    )
    assert r["ok"] is True


def test_verify_url_contains_fail():
    r = bf.verify_navigation(
        current_url="https://mail.example.com/login",
        current_text="Log in",
        expect_url_contains="/inbox",
    )
    assert r["ok"] is False


def test_verify_page_contains_fail():
    r = bf.verify_navigation(
        current_url="https://x.com",
        current_text="generic page",
        expect_page_contains="Welcome Alice",
    )
    assert r["ok"] is False


def test_verify_not_url_catches_redirect():
    r = bf.verify_navigation(
        current_url="https://site.com/login?error=1",
        current_text="",
        expect_not_url="/login",
    )
    assert r["ok"] is False


def test_verify_no_expectation_returns_ok():
    r = bf.verify_navigation(current_url="x", current_text="y")
    assert r["ok"] is True


# ── Flow runner ─────────────────────────────────────────────────────────────

def _fake_run(step, player=None, session_memory=None):
    """Reflect the step back as the output, so tests can assert what
    was executed."""
    return f"ran:{step.get('action','?')}:{step.get('url','')}"


def _probe_clean(step, player=None, session_memory=None):
    return "https://example.com/", "Hello world"


def _probe_captcha(step, player=None, session_memory=None):
    return "https://example.com/verify", "please verify you are human"


def test_run_sequence_happy_path():
    steps = [
        {"action": "go_to", "url": "https://example.com"},
        {"action": "click", "selector": "#ok"},
    ]
    with mock.patch.object(bf, "_run_browser_step", side_effect=_fake_run), \
         mock.patch.object(bf, "_probe_page", side_effect=_probe_clean):
        result = bf.run_sequence(steps)
    assert result["status"] == "ok"
    assert len(result["results"]) == 2


def test_run_sequence_pauses_on_captcha():
    steps = [
        {"action": "go_to", "url": "https://example.com"},
        {"action": "click", "selector": "#ok"},
    ]
    with mock.patch.object(bf, "_run_browser_step", side_effect=_fake_run), \
         mock.patch.object(bf, "_probe_page", side_effect=_probe_captcha):
        result = bf.run_sequence(steps)
    assert result["status"] == "captcha_pause"
    assert result["resume_step"] == 1
    assert "detector" in result


def test_run_sequence_resume_skips_prior_steps():
    steps = [
        {"action": "go_to", "url": "a"},
        {"action": "click", "selector": "b"},
        {"action": "type", "text": "c"},
    ]
    calls = []

    def recording_run(step, player=None, session_memory=None):
        calls.append(step["action"])
        return "ran"

    with mock.patch.object(bf, "_run_browser_step",
                            side_effect=recording_run), \
         mock.patch.object(bf, "_probe_page", side_effect=_probe_clean):
        bf.run_sequence(steps, resume_from=1)
    assert calls == ["click", "type"]


def test_run_sequence_verify_failure_stops_early():
    steps = [{"action": "go_to", "url": "https://x.com"}]
    verify = [{"url_contains": "/dashboard"}]
    with mock.patch.object(bf, "_run_browser_step", side_effect=_fake_run), \
         mock.patch.object(bf, "_probe_page", side_effect=_probe_clean):
        result = bf.run_sequence(steps, verify=verify)
    assert result["status"] == "verify_failed"
    assert result["failed_step"] == 0


def test_run_sequence_errors_surface():
    steps = [{"action": "go_to", "url": "x"}]
    with mock.patch.object(bf, "_run_browser_step",
                            side_effect=RuntimeError("timeout")):
        result = bf.run_sequence(steps)
    assert result["status"] == "error"
    assert "timeout" in result["error"]


# ── Action handler ──────────────────────────────────────────────────────────

def test_action_requires_steps():
    out = bf.browser_flow({"action": "run_sequence"})
    assert "'steps'" in out


def test_action_rejects_unknown():
    out = bf.browser_flow({"action": "fly"})
    assert "unknown action" in out


def test_action_run_sequence_happy_path():
    steps = json.dumps([{"action": "go_to", "url": "https://x.com"}])
    with mock.patch.object(bf, "_run_browser_step", side_effect=_fake_run), \
         mock.patch.object(bf, "_probe_page", side_effect=_probe_clean):
        out = bf.browser_flow({"action": "run_sequence", "steps": steps})
    assert "status=ok" in out


def test_action_run_sequence_pauses_on_captcha():
    steps = json.dumps([{"action": "go_to", "url": "https://x.com"}])
    with mock.patch.object(bf, "_run_browser_step", side_effect=_fake_run), \
         mock.patch.object(bf, "_probe_page", side_effect=_probe_captcha):
        out = bf.browser_flow({"action": "run_sequence", "steps": steps})
    assert "PAUSED" in out
    assert "status=captcha_pause" in out


def test_action_pause_notifies_user_via_player():
    class _FakePlayer:
        def __init__(self):
            self.logs: list[str] = []
        def write_log(self, msg):
            self.logs.append(msg)

    player = _FakePlayer()
    steps = json.dumps([{"action": "go_to", "url": "https://x.com"}])
    with mock.patch.object(bf, "_run_browser_step", side_effect=_fake_run), \
         mock.patch.object(bf, "_probe_page", side_effect=_probe_captcha):
        bf.browser_flow({"action": "run_sequence", "steps": steps},
                         player=player)
    assert any("CAPTCHA detected" in l for l in player.logs), player.logs


def test_action_wait_for_human_notifies():
    class _FakePlayer:
        def __init__(self):
            self.logs: list[str] = []
        def write_log(self, msg):
            self.logs.append(msg)

    player = _FakePlayer()
    out = bf.browser_flow({"action": "wait_for_human",
                            "note": "finish the login"},
                           player=player)
    assert "waiting_for_human" in out
    assert any("finish the login" in l for l in player.logs)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as e:
            print(f"FAIL {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    sys.exit(1 if failed else 0)
