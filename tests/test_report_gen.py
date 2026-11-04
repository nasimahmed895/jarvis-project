"""Tests for actions/report_gen.py.

Model calls are mocked. We check: skeleton fallback when no key,
structured round-trip when model returns a valid doc, section-order
preservation, path sandboxing.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import actions.report_gen as rg
from core import gemini as gem


def _mock_doc(sections):
    return {
        "title": "Mock Title",
        "subtitle": "subtitle line",
        "sections": [
            {
                "heading": s,
                "paragraphs": [f"Paragraph for {s}."],
                "bullets":    [f"bullet-{s}"],
            } for s in sections
        ],
    }


def test_skeleton_on_no_key():
    with mock.patch.object(gem, "api_key", return_value=""):
        out = rg.report_gen({"topic": "Q4 review",
                             "format": "executive_summary",
                             "save_as": "none"})
    assert "model unavailable" in out.lower()
    # Headings still present
    for section in rg._FORMATS["executive_summary"]:
        assert section in out


def test_model_round_trip_renders_markdown():
    secs = rg._FORMATS["briefing_memo"]
    with mock.patch.object(gem, "api_key", return_value="k"), \
         mock.patch.object(gem, "as_json", return_value=_mock_doc(secs)):
        out = rg.report_gen({"topic": "Partnership assessment",
                             "format": "briefing_memo",
                             "save_as": "none"})
    assert out.startswith("# Mock Title")
    for section in secs:
        assert f"## {section}" in out
    assert "bullet-Subject" in out


def test_section_override():
    with mock.patch.object(gem, "api_key", return_value="k"), \
         mock.patch.object(gem, "as_json",
                            return_value=_mock_doc(["A", "B", "C"])):
        out = rg.report_gen({
            "topic": "custom",
            "sections": "A|B|C",
            "save_as": "none",
        })
    assert "## A" in out and "## B" in out and "## C" in out


def test_section_override_json_array():
    with mock.patch.object(gem, "api_key", return_value="k"), \
         mock.patch.object(gem, "as_json",
                            return_value=_mock_doc(["X", "Y"])):
        out = rg.report_gen({
            "topic": "t",
            "sections": json.dumps(["X", "Y"]),
            "save_as": "none",
        })
    assert "## X" in out and "## Y" in out


def test_unknown_format_rejected():
    out = rg.report_gen({"topic": "x", "format": "dangling"})
    assert "unknown format" in out


def test_missing_section_filled_with_placeholder():
    # Model returns a doc missing one required section — the renderer
    # should insert the placeholder bullet rather than drop it.
    secs = rg._FORMATS["executive_summary"]
    partial = {
        "title": "T",
        "subtitle": "",
        "sections": [{"heading": secs[0], "paragraphs": ["p"], "bullets": []}],
    }
    with mock.patch.object(gem, "api_key", return_value="k"), \
         mock.patch.object(gem, "as_json", return_value=partial):
        out = rg.report_gen({"topic": "x", "save_as": "none"})
    for s in secs:
        assert f"## {s}" in out
    assert "No material yet." in out


def test_safe_output_path_rejects_outside_home(tmp_path=None):
    # Forcing a path like /etc/passwd should land in the default dir
    # under $HOME, not outside it.
    p = rg._safe_output_path("/etc/passwd", "report")
    assert str(p).startswith(str(Path.home()))


def test_save_markdown_writes_file():
    with tempfile.TemporaryDirectory() as tmp:
        # Patch $HOME so the write lands somewhere safe.
        fake_home = Path(tmp) / "home"
        fake_home.mkdir()
        with mock.patch("pathlib.Path.home", return_value=fake_home), \
             mock.patch.object(gem, "api_key", return_value="k"), \
             mock.patch.object(gem, "as_json",
                                return_value=_mock_doc(
                                    rg._FORMATS["status_report"])):
            out = rg.report_gen({
                "topic": "weekly update",
                "format": "status_report",
                "save_as": "md",
            })
        assert "Wrote" in out
        reports = list(fake_home.glob("**/*.md"))
        assert reports and reports[0].exists()


def test_rejects_missing_topic():
    assert "provide 'topic'" in rg.report_gen({})


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
