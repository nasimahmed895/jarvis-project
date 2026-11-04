"""
Emotional-cue detector and tone adapter.

The audit called out section-2 Emotional Intelligence as BROKEN —
the preview-API affective-dialog flag was dropped because the model's
own tone detection was unreliable. This module fills that gap on the
assistant side: a pure-regex, pure-lexicon classifier that reads the
user's text/transcript and returns the dominant emotional cue plus a
suggested response tone.

Deliberately simulated, not claimed. The voice loop will use
``suggested_tone`` to soften, hurry, slow down or step more carefully,
but every response is still the model's — we never put words into
the user's emotional state. The module never writes "I feel X". It
reads signals and advises tone.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

# ── Dictionaries ────────────────────────────────────────────────────────────
# Each entry maps a lowercase lexeme to a weight (1–3). Higher weights are
# for words that are only ever emotional ("furious" vs the ambiguous
# "concerned"). The matcher uses word-boundary regex so "madam" doesn't
# trip the "mad" cue.

# Entries ending with '*' are stem matches (prefix); otherwise whole-word.
# Example: "frustrat*" matches frustrated / frustration / frustrating, but
# "mad" matches only the word "mad" (not "madam" or "madrid").
_CUES: dict[str, list[tuple[str, int]]] = {
    "frustration": [
        ("frustrat*", 3), ("annoy*", 2), ("ridiculous", 3),
        ("furious", 3), ("angry", 3), ("pissed", 3), ("mad", 2),
        ("fed up", 3), ("waste of time", 3), ("useless", 2),
        ("stupid", 2), ("broken", 1), ("doesn't work", 2),
        ("not working", 2), ("terrible", 2), ("hate", 3), ("hates", 2),
        ("ugh", 2), ("come on", 1), ("seriously", 1),
    ],
    "urgency": [
        ("urgent", 3), ("asap", 3), ("now", 1), ("immediately", 3),
        ("right now", 3), ("tonight", 1), ("today", 1),
        ("deadline", 2), ("hurry", 2), ("quickly", 1),
        ("fast", 1), ("emergency", 3), ("critical", 3),
        ("need this now", 3), ("eod", 2),
    ],
    "stress": [
        ("stressed", 3), ("overwhelmed", 3), ("exhausted", 2),
        ("tired", 1), ("burnout", 3), ("too much", 2),
        ("can't do this", 3), ("drowning", 3), ("swamped", 2),
        ("buried", 2), ("anxiety", 3), ("panicking", 3),
    ],
    "confusion": [
        ("confused", 3), ("don't understand", 3), ("doesn't make sense", 3),
        ("lost", 2), ("what do you mean", 2), ("huh", 1), ("??", 1),
        ("unclear", 2), ("explain", 1), ("i don't get", 2),
        ("not sure", 1), ("wait what", 2),
    ],
    "excitement": [
        ("awesome", 3), ("amazing", 3), ("fantastic", 3), ("incredible", 3),
        ("love it", 3), ("great", 2), ("perfect", 2), ("yes!", 3),
        ("finally", 2), ("excited", 3), ("brilliant", 3), ("wonderful", 3),
        ("so happy", 3), ("thank you so much", 2), ("woohoo", 3),
    ],
    "sadness": [
        ("sad", 3), ("depressed", 3), ("down", 2), ("upset", 2),
        ("heartbroken", 3), ("miserable", 3), ("lonely", 3),
        ("lost someone", 3), ("grieving", 3),
    ],
    "gratitude": [
        ("thank you", 2), ("thanks", 1), ("appreciate*", 2),
        ("grateful", 3), ("you're the best", 2), ("saved me", 2),
    ],
}

# Written-strong punctuation signals. Multiple question-marks, lots of
# exclamations, or ALL-CAPS runs push the detector toward whichever cue
# the text already leans into — they're intensifiers, not categories of
# their own.
_INTENSIFIER_RE = re.compile(r"(!{2,}|\?{2,}|[A-Z]{4,})")


@dataclass(frozen=True)
class Reading:
    primary: str
    confidence: float
    cues: tuple[str, ...] = field(default_factory=tuple)
    scores: dict[str, int] = field(default_factory=dict)
    suggested_tone: str = "neutral"
    intensifiers: int = 0

    def to_dict(self) -> dict:
        return {
            "primary":        self.primary,
            "confidence":     round(float(self.confidence), 3),
            "cues":           list(self.cues),
            "scores":         dict(self.scores),
            "suggested_tone": self.suggested_tone,
            "intensifiers":   self.intensifiers,
        }


_TONE_FOR: dict[str, str] = {
    "frustration": "calm-and-direct",
    "urgency":     "brisk-and-action-first",
    "stress":      "gentle-and-reassuring",
    "confusion":   "step-by-step-plain",
    "excitement":  "warm-and-matching-energy",
    "sadness":     "soft-and-supportive",
    "gratitude":   "warm-brief-acknowledgement",
    "neutral":     "neutral",
}


def _score_cue(text_lower: str, patterns: Iterable[tuple[str, int]]) -> tuple[int, list[str]]:
    total = 0
    hits: list[str] = []
    for lex, weight in patterns:
        # Three shapes:
        #   "frustrat*" → stem; matches as prefix of a word
        #   "fed up"    → phrase; matches as substring (word boundaries are
        #                  implicit in the whitespace)
        #   "mad"       → whole word; must not match inside "madam"
        is_stem = lex.endswith("*")
        base = lex[:-1] if is_stem else lex
        if is_stem:
            pattern = re.compile(r"\b" + re.escape(base) + r"\w*",
                                  re.IGNORECASE)
        elif " " in base or any(ch in base for ch in "'?!"):
            pattern = re.compile(re.escape(base), re.IGNORECASE)
        else:
            pattern = re.compile(r"\b" + re.escape(base) + r"\b",
                                  re.IGNORECASE)
        found = pattern.findall(text_lower)
        if found:
            total += weight * len(found)
            hits.append(lex)
    return total, hits


def detect(text: str) -> Reading:
    """Scan ``text`` for emotional cues. Returns a Reading. For empty or
    purely informational text returns ``primary='neutral'`` at zero
    confidence — the voice loop keeps its default tone in that case."""
    text = (text or "").strip()
    if not text:
        return Reading(primary="neutral", confidence=0.0,
                       suggested_tone="neutral")
    text_lower = text.lower()
    scores: dict[str, int] = {}
    hits: list[str] = []
    for cue, patterns in _CUES.items():
        s, h = _score_cue(text_lower, patterns)
        if s:
            scores[cue] = s
            hits.extend(f"{cue}:{term}" for term in h)

    intensifiers = len(_INTENSIFIER_RE.findall(text))
    if scores:
        # Intensifiers multiply the top score — a frustrated sentence with
        # triple exclamations is very different from a calm one.
        primary = max(scores, key=lambda k: scores[k])
        if intensifiers:
            scores[primary] += intensifiers
        # Confidence: top score over sum of all scores, scaled by log-ish
        # saturation so one hit can't produce 100% confidence.
        total = sum(scores.values())
        ratio = scores[primary] / max(1, total)
        saturation = min(1.0, (scores[primary]) / 6.0)
        confidence = round(ratio * (0.3 + 0.7 * saturation), 3)
    else:
        primary = "neutral"
        confidence = 0.0

    return Reading(
        primary=primary,
        confidence=confidence,
        cues=tuple(hits),
        scores=dict(scores),
        suggested_tone=_TONE_FOR.get(primary, "neutral"),
        intensifiers=intensifiers,
    )


# ── Persistent tone preferences (stored in long-term memory) ────────────────

_PREF_CATEGORY = "preferences"
_PREF_KEY = "tone"


def remember_tone_preference(label: str) -> bool:
    """Persist a user-stated tone preference ("I like it brief", "stop
    being so formal", "slow down when explaining"). We treat the LABEL
    itself as canonical — no NLP here, just store what the user asked
    for so the voice loop can recall it and the model can see it in the
    memory block of every future session."""
    label = (label or "").strip()
    if not label:
        return False
    try:
        from memory.memory_manager import update_memory
        update_memory({_PREF_CATEGORY: {_PREF_KEY: {"value": label}}})
    except Exception as e:
        print(f"[Emotion] remember_tone_preference failed: {e}")
        return False
    return True


def recall_tone_preference() -> str | None:
    try:
        from memory.memory_manager import load_memory
        mem = load_memory()
        entry = (mem.get(_PREF_CATEGORY) or {}).get(_PREF_KEY)
        if isinstance(entry, dict):
            val = str(entry.get("value") or "").strip()
            return val or None
    except Exception as e:
        print(f"[Emotion] recall_tone_preference failed: {e}")
    return None
