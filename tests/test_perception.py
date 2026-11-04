"""Tests for core/perception.py.

The real backends (pytesseract, Gemini vision) aren't available in the
unit environment. We mock both to assert the routing logic — pytesseract
is preferred for OCR, Gemini is called for everything else, and absent
backends produce a clean fallback instead of crashing.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import perception as pc


def test_analyze_rejects_empty_bytes():
    obs = pc.analyze_image(b"", "image/png", mode="describe").to_dict()
    assert obs["source"] == "fallback"
    assert "no image bytes" in (obs["error"] or "")


def test_answer_mode_without_question():
    obs = pc.analyze_image(b"\x00\x01", "image/png", mode="answer").to_dict()
    assert obs["source"] == "fallback"
    assert "needs a question" in (obs["error"] or "")


def test_ocr_prefers_pytesseract():
    """When pytesseract returns text, we never reach Gemini."""
    with mock.patch.object(pc, "_try_pytesseract_ocr",
                            return_value="hello world"), \
         mock.patch.object(pc, "_gemini_vision") as gem:
        obs = pc.analyze_image(b"\x89PNG", "image/png", mode="ocr").to_dict()
    assert obs["source"] == "pytesseract"
    assert obs["text"] == "hello world"
    gem.assert_not_called()


def test_ocr_falls_back_to_gemini_when_pytesseract_empty():
    with mock.patch.object(pc, "_try_pytesseract_ocr", return_value=None), \
         mock.patch.object(pc, "_gemini_vision",
                            return_value="Gemini-extracted text"):
        obs = pc.analyze_image(b"\x89PNG", "image/png", mode="ocr").to_dict()
    assert obs["source"] == "gemini"
    assert "Gemini-extracted" in obs["text"]


def test_ocr_fallback_when_no_backend():
    with mock.patch.object(pc, "_try_pytesseract_ocr", return_value=None), \
         mock.patch.object(pc, "_gemini_vision", return_value=None):
        obs = pc.analyze_image(b"\x89PNG", "image/png", mode="ocr").to_dict()
    assert obs["source"] == "fallback"


def test_describe_calls_gemini():
    with mock.patch.object(pc, "_gemini_vision",
                            return_value="A dog in a field."):
        obs = pc.analyze_image(b"\x89PNG", "image/png",
                                mode="describe").to_dict()
    assert obs["source"] == "gemini"
    assert "dog" in obs["caption"]


def test_objects_mode_parses_bulleted_output():
    raw = "- Laptop\n- Mug\n- Notebook\nOverall: a desk setup."
    with mock.patch.object(pc, "_gemini_vision", return_value=raw):
        obs = pc.analyze_image(b"X", "image/png", mode="objects").to_dict()
    assert obs["objects"] == ["Laptop", "Mug", "Notebook"]
    assert "desk setup" in obs["caption"]


def test_answer_mode_round_trip():
    with mock.patch.object(pc, "_gemini_vision",
                            return_value="It's a Toyota."):
        obs = pc.analyze_image(b"X", "image/png", mode="answer",
                                question="What car is this?").to_dict()
    assert obs["source"] == "gemini"
    assert obs["answer"] == "It's a Toyota."


def test_bytes_hash_stable_across_calls():
    # Both calls mocked — this test is about the hash function, not the
    # vision backend. Without the mock the first call races to Gemini,
    # which spits a 400 on 8-byte "same" and spams the test output.
    with mock.patch.object(pc, "_gemini_vision", return_value="x"):
        obs1 = pc.analyze_image(b"same", "image/png",
                                 mode="describe").to_dict()
        obs2 = pc.analyze_image(b"same", "image/png",
                                 mode="describe").to_dict()
    assert obs1["bytes_hash"] == obs2["bytes_hash"]


def test_screenshot_capture_wraps_errors():
    with mock.patch.object(pc, "capture_screenshot",
                            side_effect=RuntimeError("no mss")):
        obs = pc.screenshot_and_analyze(mode="ui").to_dict()
    assert obs["source"] == "fallback"
    assert "screen capture" in (obs["error"] or "")


def test_camera_capture_wraps_errors():
    with mock.patch.object(pc, "capture_camera",
                            side_effect=RuntimeError("no cam")):
        obs = pc.camera_and_analyze(mode="describe").to_dict()
    assert obs["source"] == "fallback"
    assert "camera capture" in (obs["error"] or "")


def test_load_image_file_detects_mime(tmp_path=None):
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
        f.write(b"\xff\xd8\xff\xe0" + b"pretend-jpeg")
        path = f.name
    data, mime = pc.load_image_file(path)
    assert mime == "image/jpeg"
    assert data.startswith(b"\xff\xd8")


def test_load_image_file_missing_raises():
    try:
        pc.load_image_file("/no/such/path.png")
    except FileNotFoundError:
        return
    raise AssertionError("expected FileNotFoundError")


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
