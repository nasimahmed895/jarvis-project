"""Tests for core/decide.py.

Mocks core.gemini.as_json so tests work without a live API key.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import decide as dec
from core import gemini as gem
import actions.decide as dec_action


def _mock_resp(options=None):
    return {
        "recommendation": "Option A",
        "confidence": 0.82,
        "ranked": [
            {"option": "Option A", "score": 0.82,
             "pros": ["fast", "cheap"], "cons": ["new"], "risk": "unknown team"},
            {"option": "Option B", "score": 0.6,
             "pros": ["proven"], "cons": ["expensive"], "risk": "slow"},
        ],
        "rationale": "A is faster and cheaper with acceptable risk.",
        "assumptions": ["budget constraint active"],
        "unknowns": ["actual latency of A"],
        "speech": "I recommend Option A — faster and cheaper.",
    }


def test_decide_returns_structured_dict():
    with mock.patch.object(gem, "api_key", return_value="fake-key"), \
         mock.patch.object(gem, "as_json", return_value=_mock_resp()):
        out = dec.decide(
            "Pick A or B?",
            options=["Option A", "Option B"],
            criteria=["speed", "cost"],
        )
    assert out["recommendation"] == "Option A"
    assert out["source"] == "model"
    assert len(out["ranked"]) == 2
    assert 0.0 <= out["confidence"] <= 1.0


def test_decide_clamps_bad_scores():
    bad = {
        "recommendation": "X",
        "confidence": 2.5,
        "ranked": [{"option": "X", "score": -3.0, "pros": [], "cons": [],
                    "risk": ""}],
        "rationale": "",
        "assumptions": [],
        "unknowns": [],
        "speech": "",
    }
    with mock.patch.object(gem, "api_key", return_value="fake"), \
         mock.patch.object(gem, "as_json", return_value=bad):
        out = dec.decide("Q")
    assert out["confidence"] == 1.0
    assert out["ranked"][0]["score"] == 0.0


def test_decide_fallback_without_key():
    with mock.patch.object(gem, "api_key", return_value=""):
        out = dec.decide("Should I ship today?", options=["yes", "no"])
    assert out["source"] == "fallback"
    assert "no Gemini key" in (out["error"] or "")
    assert len(out["ranked"]) == 2
    assert out["recommendation"] == "yes"


def test_decide_fallback_on_empty_question():
    out = dec.decide("")
    assert out["source"] == "fallback"
    assert out["error"] == "empty question"


def test_decide_fallback_on_model_none():
    with mock.patch.object(gem, "api_key", return_value="k"), \
         mock.patch.object(gem, "as_json", return_value=None):
        out = dec.decide("Q")
    assert out["source"] == "fallback"
    assert "returned nothing" in (out["error"] or "")


def test_decide_fallback_on_non_dict():
    with mock.patch.object(gem, "api_key", return_value="k"), \
         mock.patch.object(gem, "as_json", return_value="just a string"):
        out = dec.decide("Q")
    assert out["source"] == "fallback"


def test_action_decide_formats_output():
    with mock.patch.object(gem, "api_key", return_value="k"), \
         mock.patch.object(gem, "as_json", return_value=_mock_resp()):
        s = dec_action.decide({"question": "A vs B", "options": "A | B"})
    assert "Recommendation: Option A" in s
    assert "confidence 0.82" in s
    assert "+ fast" in s
    assert "- new" in s


def test_action_decide_rejects_empty_question():
    assert "provide 'question'" in dec_action.decide({"question": ""})


def test_action_decide_parses_json_options():
    with mock.patch.object(gem, "api_key", return_value="k"), \
         mock.patch.object(gem, "as_json", return_value=_mock_resp()):
        s = dec_action.decide({
            "question": "pick",
            "options": '["Option A", "Option B"]',
        })
    assert "Recommendation" in s


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
