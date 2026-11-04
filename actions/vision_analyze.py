"""
Vision analysis action — describe / OCR / objects / UI / Q&A on an image.

Unified entry point over ``core/perception.py``. Complements the live
vision_stream and the one-shot ``screen_process`` tool in main.py — this
one gives the model a single API for every analysis mode and every
source (screenshot, camera, file).
"""
from __future__ import annotations

from typing import Any

from core import perception as _p
from core.context_buffer import record  # populated by P5.3 import even if empty


def _fmt(obs: dict) -> str:
    mode = obs.get("mode", "?")
    source = obs.get("source", "?")
    if obs.get("error"):
        return f"vision_analyze[{mode}] {source}: {obs['error']}"
    lines = [f"vision_analyze[{mode}] source={source} "
             f"confidence={obs.get('confidence', 0.0):.2f}"]
    if obs.get("caption"):
        lines.append(obs["caption"])
    if obs.get("objects"):
        lines.append("Objects:")
        for o in obs["objects"][:12]:
            lines.append(f"  - {o}")
    if obs.get("text"):
        lines.append("---")
        lines.append(obs["text"])
    if obs.get("answer"):
        lines.append("Answer: " + obs["answer"])
    return "\n".join(lines)


def vision_analyze(
    parameters:    dict | None = None,
    response: Any = None,
    player:   Any = None,
    session_memory: Any = None,
) -> str:
    """Analyze an image from screen / camera / file.

    Fields:
      source   screen (default) | camera | file
      path     required when source=file
      mode     auto | describe | ocr | objects | ui | answer
      question required when mode=answer
    """
    p = parameters or {}
    source = str(p.get("source") or "screen").lower().strip()
    mode = str(p.get("mode") or "auto").lower().strip()
    question = p.get("question") or None

    if mode not in ("auto", "describe", "ocr", "objects", "ui", "answer"):
        return (f"vision_analyze: unknown mode {mode!r}. "
                "Modes: auto | describe | ocr | objects | ui | answer")

    if source == "screen":
        obs = _p.screenshot_and_analyze(mode=mode, question=question)
    elif source == "camera":
        obs = _p.camera_and_analyze(mode=mode, question=question)
    elif source == "file":
        path = str(p.get("path") or "").strip()
        if not path:
            return "vision_analyze: file source needs 'path'."
        try:
            data, mime = _p.load_image_file(path)
        except FileNotFoundError:
            return f"vision_analyze: file not found: {path}"
        except Exception as e:
            return f"vision_analyze: load failed ({type(e).__name__}: {e})"
        obs = _p.analyze_image(data, mime, mode=mode, question=question)
    else:
        return (f"vision_analyze: unknown source {source!r}. "
                "Sources: screen | camera | file")

    obs_dict = obs.to_dict()
    # Drop a condensed summary into the multimodal context buffer so
    # follow-up questions ("what was that logo?") can reference it.
    try:
        kind = f"vision:{source}:{mode}"
        summary = (obs_dict.get("caption")
                    or obs_dict.get("answer")
                    or obs_dict.get("text", "")[:200]
                    or ("objects: " + ", ".join(obs_dict.get("objects", [])[:5]))
                    or "(empty)")
        record(kind, summary, meta={"source": obs_dict.get("source"),
                                     "confidence": obs_dict.get("confidence")})
    except Exception:
        # Context buffer is best-effort; vision analysis must never fail
        # because the buffer is off / missing.
        pass
    return _fmt(obs_dict)


TOOL = {
    "name": "vision_analyze",
    "description": (
        "Analyze an image and return a structured description, OCR text, "
        "object list, UI breakdown, or direct answer. Sources: 'screen' "
        "(current display), 'camera' (webcam), 'file' (local path). "
        "Modes: 'auto' (short mixed description), 'describe', 'ocr' "
        "(verbatim text extraction, tries local pytesseract first then "
        "Gemini vision), 'objects', 'ui' (app / screen breakdown), "
        "'answer' (needs a 'question'). Captured observations are "
        "logged into the multimodal context buffer so later turns can "
        "reference 'what you just saw'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "source":   {"type": "STRING",
                         "description": "screen | camera | file"},
            "path":     {"type": "STRING",
                         "description": "Local image path (for source=file)."},
            "mode":     {"type": "STRING",
                         "description": "auto | describe | ocr | objects | ui | answer"},
            "question": {"type": "STRING",
                         "description": "Question to answer (for mode=answer)."},
        },
        "required": [],
    },
    "handler": vision_analyze,
}
