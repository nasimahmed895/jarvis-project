"""
Smoke tests for the new module actions: jobs, real_estate, education,
customer_support, social_media.

These tests exercise argument validation, approval gating, and TOOL schema
shape. They deliberately do NOT make network calls — web_search is stubbed
and the LLM call is stubbed to a fixed reply. Testing the real browser path
belongs in tests/integration/, where a controlled test site is available.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from actions import jobs, real_estate, education, customer_support, social_media
from core import approval, confirm


@pytest.fixture(autouse=True)
def _reset_confirm():
    # LVL2/LVL3 branches use core/confirm. In tests we leave the HUD
    # unbound so the gate returns runnable results instead of parking a
    # banner. Each test resets.
    confirm._show_cb = None
    confirm._hide_cb = None
    confirm._pending = None
    yield
    confirm._show_cb = None
    confirm._hide_cb = None
    confirm._pending = None


@pytest.fixture
def stub_gemini(monkeypatch):
    def _fake_text(_prompt, *a, **kw):
        return "STUB-OK"
    monkeypatch.setattr("core.gemini.text", _fake_text)
    yield


@pytest.fixture
def stub_web_search(monkeypatch):
    calls = []

    def _fake(params, player=None, **kw):
        calls.append(params)
        return "RESULTS: " + str(params.get("query") or params.get("items"))

    monkeypatch.setattr("actions.jobs._web_search", _fake)
    monkeypatch.setattr("actions.real_estate._web_search", _fake)
    monkeypatch.setattr("actions.education._web_search", _fake)
    monkeypatch.setattr("actions.customer_support._web_search", _fake)
    return calls


# ── jobs ──────────────────────────────────────────────────────────────

def test_jobs_tool_schema():
    assert jobs.TOOL["name"] == "jobs"
    assert "action" in jobs.TOOL["parameters"]["properties"]
    assert jobs.TOOL["handler"] is jobs.jobs


def test_jobs_search_builds_query(stub_web_search):
    out = jobs.jobs({"action": "search", "title": "Python engineer",
                     "location": "Berlin", "remote": True, "limit": 5})
    assert "RESULTS" in out
    # web_search received a well-formed query
    sent = stub_web_search[-1]
    assert "Python engineer" in sent["query"]
    assert "Berlin" in sent["query"]
    assert "remote" in sent["query"]


def test_jobs_apply_gates_without_hud():
    # No HUD bound → LVL3 gate refuses, returning a safe-fallback sentence.
    out = jobs.jobs({"action": "apply", "url": "https://example.com/job/1"})
    assert "cannot confirm" in out.lower() or "not done" in out.lower()


def test_jobs_draft_letter_uses_llm(stub_gemini):
    out = jobs.jobs({"action": "draft_letter",
                     "listing": "Looking for a senior Python dev"})
    assert out == "STUB-OK"


def test_jobs_save_resume_and_match(tmp_path, monkeypatch, stub_gemini):
    monkeypatch.setattr(jobs, "_RESUME_PATH", tmp_path / "resume.json")
    jobs.jobs({"action": "save_resume",
               "resume": {"name": "X", "skills": ["Python", "SQL"]}})
    out = jobs.jobs({"action": "match", "listing": "Need Python + SQL"})
    assert out == "STUB-OK"


# ── real_estate ────────────────────────────────────────────────────────

def test_real_estate_search(stub_web_search):
    out = real_estate.real_estate({"action": "search", "kind": "apartment",
                                   "location": "Lisbon", "bedrooms": 2,
                                   "budget_max": 1500, "for_rent": True})
    assert "RESULTS" in out
    sent = stub_web_search[-1]
    assert "apartment" in sent["query"]
    assert "Lisbon" in sent["query"]
    assert "for rent" in sent["query"]


def test_real_estate_compare_requires_items():
    out = real_estate.real_estate({"action": "compare"})
    assert "items" in out


# ── education ─────────────────────────────────────────────────────────

def test_education_study_plan_uses_llm(stub_gemini):
    out = education.education({"action": "study_plan", "goal": "Rust",
                               "weeks": 4, "hours_per_week": 6})
    assert out == "STUB-OK"


def test_education_requires_topic():
    out = education.education({"action": "search_course"})
    assert "topic" in out.lower()


# ── customer_support ───────────────────────────────────────────────────

def test_support_draft_ticket(stub_gemini):
    out = customer_support.customer_support(
        {"action": "draft_ticket", "product": "WidgetCo",
         "problem": "Login fails on Android", "severity": "P2",
         "repro_steps": "1) open app  2) tap Sign in"})
    assert out == "STUB-OK"


def test_support_open_ticket_lvl2_without_hud():
    out = customer_support.customer_support(
        {"action": "open_ticket",
         "portal_url": "https://support.widgetco.example/new",
         "draft": "Summary: login fails"})
    # With no HUD bound and no stored auto-preference, LVL2 falls through
    # and the preparation runs — we assert the returned sentence references
    # the portal.
    assert "support.widgetco.example" in out


# ── social_media ───────────────────────────────────────────────────────

def test_social_draft_trims_to_platform_limit(stub_gemini, monkeypatch):
    long_reply = "x" * 1000
    monkeypatch.setattr("core.gemini.text", lambda *a, **k: long_reply)
    out = social_media.social_media({"action": "draft",
                                     "platform": "twitter",
                                     "topic": "Launch day"})
    # Twitter hard-trim is 275.
    assert len(out) <= 275


def test_social_publish_without_hud_passes_through(stub_gemini):
    # LVL2 + no HUD → gate falls through so the model gets a concrete plan.
    out = social_media.social_media({"action": "publish",
                                     "platform": "linkedin",
                                     "draft": "Hello world."})
    assert "linkedin" in out.lower()
    assert "Hello world." in out
