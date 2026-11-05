"""Tests for Gemini model failure classification and fallback."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import gemini as gem


def test_live_1011_quota_error_cools_model_and_selects_fallback(monkeypatch):
    monkeypatch.setattr(gem, "_cooldown", {})
    error = (
        "1011 None. You exceeded your current quota, "
        "please check your plan and billing details."
    )

    assert gem.note_live_failure(gem.LIVE_MODELS[0], error)
    assert gem.live_model() == gem.LIVE_MODELS[1]


def test_generic_1011_error_is_not_treated_as_quota():
    assert not gem.is_quota_error("1011 internal server error")
