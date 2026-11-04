"""Tests for core/emotion.py + actions/emotion_adapt.py."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import emotion as em
import actions.emotion_adapt as ea


def test_empty_text_neutral():
    r = em.detect("").to_dict()
    assert r["primary"] == "neutral"
    assert r["confidence"] == 0.0


def test_frustration_cues():
    r = em.detect("This is ridiculous and completely useless!").to_dict()
    assert r["primary"] == "frustration"
    assert r["suggested_tone"] == "calm-and-direct"
    assert r["confidence"] > 0.3


def test_urgency_cues():
    r = em.detect("I need this ASAP, deadline is EOD").to_dict()
    assert r["primary"] == "urgency"
    assert r["suggested_tone"] == "brisk-and-action-first"


def test_stress_cues():
    r = em.detect("I'm so overwhelmed and exhausted I can't do this").to_dict()
    assert r["primary"] == "stress"
    assert r["suggested_tone"] == "gentle-and-reassuring"


def test_confusion_cues():
    r = em.detect("Wait what?? I don't understand what you mean").to_dict()
    assert r["primary"] == "confusion"
    assert r["suggested_tone"] == "step-by-step-plain"


def test_excitement_cues():
    r = em.detect("Finally! This is amazing, I love it!!").to_dict()
    assert r["primary"] == "excitement"
    assert r["intensifiers"] >= 1


def test_gratitude_cues():
    r = em.detect("Thank you so much, you saved me.").to_dict()
    assert r["primary"] == "gratitude"


def test_sadness_cues():
    r = em.detect("I'm feeling really depressed and lonely lately.").to_dict()
    assert r["primary"] == "sadness"


def test_word_boundary_prevents_false_positive():
    # 'mad' shouldn't match inside 'madam' or 'madrid'.
    r = em.detect("Hello, madam. Are you from Madrid?").to_dict()
    assert r["primary"] == "neutral"


def test_intensifiers_bump_confidence():
    base = em.detect("I'm frustrated").to_dict()
    boosted = em.detect("I'M FRUSTRATED!!!").to_dict()
    assert boosted["confidence"] > base["confidence"]
    assert boosted["intensifiers"] >= 2


def test_remember_tone_preference_round_trip():
    import memory.memory_manager as mm
    tmpdir = Path(tempfile.mkdtemp())
    with mock.patch.object(mm, "MEMORY_PATH", tmpdir / "long_term.json"), \
         mock.patch.object(mm, "BACKUP_PATH", tmpdir / "bak"):
        assert em.remember_tone_preference("keep replies brief")
        assert em.recall_tone_preference() == "keep replies brief"


def test_action_detect():
    out = ea.emotion_adapt({"action": "detect",
                             "text": "this is urgent!"})
    assert "primary=urgency" in out


def test_action_rejects_empty():
    assert "provide 'text'" in ea.emotion_adapt({"action": "detect"})


def test_action_remember_round_trip():
    import memory.memory_manager as mm
    tmpdir = Path(tempfile.mkdtemp())
    with mock.patch.object(mm, "MEMORY_PATH", tmpdir / "long_term.json"), \
         mock.patch.object(mm, "BACKUP_PATH", tmpdir / "bak"):
        out = ea.emotion_adapt({"action": "remember_preference",
                                 "label": "less formal"})
        assert "Remembered" in out
        out2 = ea.emotion_adapt({"action": "recall_preference"})
        assert "less formal" in out2


def test_action_unknown():
    assert "unknown action" in ea.emotion_adapt({"action": "warp"})


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
