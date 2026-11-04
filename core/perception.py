"""
Perception core — unified multimodal image understanding.

Fills the audit's section-5 gap (no coherent vision pipeline beyond the
live Gemini stream and one-shot screen_process). Wraps three input
sources (screenshot / camera / arbitrary file) behind one analyzer that
can:

  * describe the scene
  * do OCR (via pytesseract when installed, otherwise via Gemini vision)
  * identify objects
  * interpret a UI screenshot
  * answer a question about the image

Every mode is a single function call returning a structured dict so the
action layer and the model's tool calls never need to care which backend
answered. When no vision backend is reachable (no key, no mss, no
image) we return a labelled fallback — never fabrication.
"""
from __future__ import annotations

import base64
import io
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional

try:
    from core import gemini as _gem  # type: ignore
except Exception:  # pragma: no cover
    _gem = None  # type: ignore[assignment]


Mode = Literal["auto", "describe", "ocr", "objects", "ui", "answer"]


_SCENE_PROMPT = {
    "describe": (
        "Describe the scene in this image. Be factual: name what you can "
        "see, the approximate setting, visible text, and anything notable. "
        "Avoid embellishment and avoid guessing identity of specific people."
    ),
    "objects": (
        "List the salient objects visible in this image as a bulleted "
        "list. One object per line. Omit trivial decor. End the list "
        "with a one-sentence note on the overall scene type."
    ),
    "ui": (
        "This is a screenshot of a computer screen or application window. "
        "Describe the app/site, list the main UI elements (buttons, "
        "menus, inputs, notifications), and summarise what state the "
        "interface appears to be in."
    ),
    "ocr": (
        "Extract the readable text from this image, verbatim. Preserve "
        "line breaks and reading order. If part of the text is illegible "
        "or you are unsure, mark it `[unclear]` rather than guessing. "
        "Return ONLY the extracted text."
    ),
    "answer": (
        "Answer the user's question strictly based on the image. If the "
        "answer isn't visible, say so. Keep it short."
    ),
    "auto": (
        "Give a short description of the image, the salient objects, and "
        "any clearly visible text, in that order. If there's a screen, "
        "name the app."
    ),
}


@dataclass
class Observation:
    mode: Mode
    source: str              # gemini | pytesseract | fallback | screenshot | camera
    caption: str = ""
    text: str = ""           # OCR output
    objects: list[str] = field(default_factory=list)
    answer: str = ""
    confidence: float = 0.0
    error: Optional[str] = None
    bytes_hash: str = ""     # short hash so callers can dedupe

    def to_dict(self) -> dict:
        return {
            "mode":       self.mode,
            "source":     self.source,
            "caption":    self.caption,
            "text":       self.text,
            "objects":    list(self.objects),
            "answer":     self.answer,
            "confidence": round(float(self.confidence), 3),
            "error":      self.error,
            "bytes_hash": self.bytes_hash,
        }


def _hash_bytes(data: bytes) -> str:
    import hashlib
    return hashlib.sha1(data or b"").hexdigest()[:12]


# ── Capture helpers ─────────────────────────────────────────────────────────

def capture_screenshot() -> tuple[bytes, str]:
    """Reuse the existing screen capture path so vision and the live
    stream pick up the same frame-compression settings."""
    from actions.screen_processor import _capture_screen
    return _capture_screen()


def capture_camera() -> tuple[bytes, str]:
    from actions.screen_processor import _capture_camera
    return _capture_camera()


def load_image_file(path: str | Path) -> tuple[bytes, str]:
    p = Path(path).expanduser()
    if not p.exists() or not p.is_file():
        raise FileNotFoundError(str(p))
    ext = p.suffix.lower().lstrip(".")
    mime_map = {
        "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "png": "image/png",  "gif": "image/gif",
        "webp": "image/webp", "bmp": "image/bmp",
    }
    mime = mime_map.get(ext, "application/octet-stream")
    return p.read_bytes(), mime


# ── OCR (pytesseract if installed) ──────────────────────────────────────────

def _try_pytesseract_ocr(image_bytes: bytes) -> str | None:
    """Return OCR text if pytesseract is installed AND the ``tesseract``
    binary is on PATH; otherwise None so the caller falls back to Gemini."""
    try:
        import pytesseract  # type: ignore
        from PIL import Image  # type: ignore
    except Exception:
        return None
    try:
        img = Image.open(io.BytesIO(image_bytes))
        text = pytesseract.image_to_string(img)
        text = (text or "").strip()
        return text or None
    except Exception as e:
        print(f"[Perception] pytesseract ocr failed: {e}")
        return None


# ── Gemini vision ───────────────────────────────────────────────────────────

def _gemini_vision(image_bytes: bytes, mime: str, *,
                   prompt: str,
                   tier: str | None = None,
                   timeout_ms: int = 25_000) -> str | None:
    if _gem is None or not getattr(_gem, "api_key", lambda: "")():
        return None
    try:
        from google.genai import types as _types  # type: ignore
    except Exception as e:
        print(f"[Perception] google-genai types unavailable: {e}")
        return None
    try:
        part = _types.Part.from_bytes(data=image_bytes, mime_type=mime)
    except Exception:
        # Older SDK variants.
        try:
            part = _types.Part(
                inline_data={"mime_type": mime,
                             "data": base64.b64encode(image_bytes).decode("ascii")}
            )
        except Exception as e:
            print(f"[Perception] part construction failed: {e}")
            return None
    try:
        return _gem.call(
            [prompt, part],
            tier=tier or getattr(_gem, "SMART", None) or "smart",
            timeout_ms=timeout_ms,
        )
    except Exception as e:
        print(f"[Perception] gemini vision call failed: {e}")
        return None


# ── Parsers ─────────────────────────────────────────────────────────────────

def _parse_objects_output(raw: str) -> tuple[list[str], str]:
    """Pull a bullet list out of the model's object-list reply.
    Everything after the bullets is treated as the trailing scene note."""
    if not raw:
        return [], ""
    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    objects: list[str] = []
    scene_lines: list[str] = []
    seen_bullets = False
    for line in lines:
        stripped = line.lstrip("-*•· \t")
        if line != stripped:
            seen_bullets = True
            objects.append(stripped.strip(":").strip())
        elif seen_bullets:
            scene_lines.append(line)
    if not objects:
        # The model may have returned prose instead; split by commas.
        chunk = ", ".join(scene_lines or [raw])
        objects = [c.strip() for c in chunk.split(",") if c.strip()][:10]
    return objects[:20], " ".join(scene_lines)[:400]


# ── Public API ──────────────────────────────────────────────────────────────

def analyze_image(image_bytes: bytes, mime: str, *,
                  mode: Mode = "auto",
                  question: str | None = None) -> Observation:
    """Run the requested mode and return an Observation.

    ``mode='answer'`` requires a question; ``mode='ocr'`` tries
    pytesseract first and falls back to Gemini.
    """
    bytes_hash = _hash_bytes(image_bytes)
    if not image_bytes:
        return Observation(mode=mode, source="fallback",
                           error="no image bytes", bytes_hash=bytes_hash)

    if mode == "answer" and not question:
        return Observation(mode=mode, source="fallback",
                           error="answer mode needs a question",
                           bytes_hash=bytes_hash)

    # OCR: try pytesseract before Gemini. This avoids a network trip for
    # the most common case ("read this receipt").
    if mode == "ocr":
        text = _try_pytesseract_ocr(image_bytes)
        if text:
            return Observation(mode="ocr", source="pytesseract",
                               text=text, confidence=0.85,
                               bytes_hash=bytes_hash)
        prompt = _SCENE_PROMPT["ocr"]
        raw = _gemini_vision(image_bytes, mime, prompt=prompt)
        if raw is None:
            return Observation(mode="ocr", source="fallback",
                               error="no OCR backend reachable",
                               bytes_hash=bytes_hash)
        return Observation(mode="ocr", source="gemini",
                           text=raw.strip(), confidence=0.75,
                           bytes_hash=bytes_hash)

    # Describe / objects / ui / answer / auto — all via Gemini vision.
    base_prompt = _SCENE_PROMPT.get(mode, _SCENE_PROMPT["auto"])
    if mode == "answer":
        prompt = f"{base_prompt}\n\nQuestion: {question}"
    else:
        prompt = base_prompt
    raw = _gemini_vision(image_bytes, mime, prompt=prompt)
    if raw is None:
        return Observation(mode=mode, source="fallback",
                           error="vision backend unreachable",
                           bytes_hash=bytes_hash)

    if mode == "objects":
        objs, scene = _parse_objects_output(raw)
        return Observation(mode=mode, source="gemini",
                           objects=objs, caption=scene,
                           confidence=0.75,
                           bytes_hash=bytes_hash)
    if mode == "answer":
        return Observation(mode=mode, source="gemini",
                           answer=raw.strip(), confidence=0.75,
                           bytes_hash=bytes_hash)
    # describe / ui / auto all collapse into a caption string.
    return Observation(mode=mode, source="gemini",
                       caption=raw.strip(), confidence=0.75,
                       bytes_hash=bytes_hash)


# ── One-shot helpers that bundle capture + analysis ─────────────────────────

def screenshot_and_analyze(*, mode: Mode = "ui",
                            question: str | None = None) -> Observation:
    try:
        data, mime = capture_screenshot()
    except Exception as e:
        return Observation(mode=mode, source="fallback",
                           error=f"screen capture failed: {e}")
    return analyze_image(data, mime, mode=mode, question=question)


def camera_and_analyze(*, mode: Mode = "describe",
                        question: str | None = None) -> Observation:
    try:
        data, mime = capture_camera()
    except Exception as e:
        return Observation(mode=mode, source="fallback",
                           error=f"camera capture failed: {e}")
    return analyze_image(data, mime, mode=mode, question=question)
