"""
Emotion action — detect cues, suggest tone, remember user preferences.

Thin surface on top of ``core/emotion.py``. The voice loop can call
``detect`` on every user utterance (it's offline and microseconds) and
read ``suggested_tone`` to decide pacing / warmth. The model itself
handles the actual phrasing — we never dictate the words, only the
posture.

JARVIS does NOT claim to feel anything. The TOOL description below
makes that boundary explicit so the model doesn't drift into "I'm
sorry you feel that way" theatre; the point is to be more useful when
the user is stressed, not to simulate empathy as fiction.
"""
from __future__ import annotations

from typing import Any

from core import emotion as _em


def _fmt(r: dict) -> str:
    bits = [
        f"primary={r.get('primary','neutral')}",
        f"confidence={r.get('confidence',0.0):.2f}",
        f"tone={r.get('suggested_tone','neutral')}",
    ]
    cues = r.get("cues") or []
    if cues:
        bits.append("cues=" + ",".join(cues[:5]))
    inten = int(r.get("intensifiers") or 0)
    if inten:
        bits.append(f"intensifiers={inten}")
    return " · ".join(bits)


def emotion_adapt(
    parameters:    dict | None = None,
    response: Any = None,
    player:   Any = None,
    session_memory: Any = None,
) -> str:
    """Detect emotional cues and (optionally) remember tone preferences.

    Actions:
      detect                 text=<utterance>
      remember_preference    label=<e.g. "keep replies brief", "softer tone">
      recall_preference      — return the stored preference (if any)
      clear_preference       — remove the stored preference
    """
    p = parameters or {}
    action = str(p.get("action", "detect")).lower().strip()

    if action == "detect":
        text = str(p.get("text") or "").strip()
        if not text:
            return "emotion_adapt detect: provide 'text'."
        r = _em.detect(text).to_dict()
        return _fmt(r)

    if action == "remember_preference":
        label = str(p.get("label") or "").strip()
        if not label:
            return "emotion_adapt remember_preference: provide 'label'."
        if _em.remember_tone_preference(label):
            return f"Remembered tone preference: {label!r}"
        return "Could not save tone preference."

    if action == "recall_preference":
        pref = _em.recall_tone_preference()
        return f"Tone preference: {pref}" if pref else "No tone preference stored."

    if action == "clear_preference":
        try:
            from memory.memory_manager import forget
            msg = forget("tone", category="preferences")
            return str(msg)
        except Exception as e:
            return f"clear_preference: {e}"

    return (f"emotion_adapt: unknown action {action!r}. "
            "Actions: detect | remember_preference | recall_preference | "
            "clear_preference")


TOOL = {
    "name": "emotion_adapt",
    "description": (
        "Read emotional cues from the user's own words and pick a "
        "response TONE — never claim feelings. 'detect' returns a "
        "primary cue (frustration | urgency | stress | confusion | "
        "excitement | sadness | gratitude | neutral), a confidence, "
        "the cue tokens that matched, and a suggested tone label. Use "
        "this on tricky turns before composing a reply: if the user is "
        "frustrated, keep the reply calm and direct; if they're stressed, "
        "lead with reassurance. 'remember_preference' persists a "
        "user-stated preference ('keep replies brief', 'less formal'); "
        "the preference rides in every future system prompt. Offline, "
        "fast, never claims to FEEL — simulates attentiveness."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "detect | remember_preference | recall_preference | clear_preference"},
            "text":   {"type": "STRING", "description": "User utterance (for 'detect')."},
            "label":  {"type": "STRING", "description": "Tone preference label (for 'remember_preference')."},
        },
        "required": ["action"],
    },
    "handler": emotion_adapt,
}
