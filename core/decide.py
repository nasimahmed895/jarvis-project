"""
Decision support engine.

The audit called out Decision Support as a CRITICAL gap: JARVIS answered,
executed, and even planned — but never ranked options for the user. This
module closes that gap with a single function, ``decide()``, that takes a
question (plus optional explicit options, criteria, and context) and
returns a structured recommendation: ranked options, pros/cons, confidence,
unknowns, and a rationale sentence.

Design
------
* The hard reasoning goes to Gemini SMART through ``gemini.as_json``. That
  keeps the structure schema strict (no "maybe"), and reuses the same
  fallback ladder the rest of the app depends on.
* When no model is reachable (no key, quota, network down), a pure-Python
  fallback returns a minimally-useful structure explaining that the model
  was unavailable. Callers always get a well-formed dict.
* The result is designed for voice output: ``speech`` is a single ready-to-
  read sentence, so the planner/model can speak the verdict directly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

try:
    from core import gemini as _gem  # type: ignore
except Exception:  # pragma: no cover — gemini is a hard dep but keep safe
    _gem = None  # type: ignore[assignment]


# System prompt. Emphasises STRUCTURED output, HONESTY about unknowns, and
# keeps the response bounded so callers can speak it without truncation.
_SYSTEM = """\
You are the decision brain of a voice assistant.

Given a DECISION REQUEST, produce a single JSON object:

{
  "recommendation": "<the option (or synthesised path) you recommend>",
  "confidence": <float 0.0-1.0>,
  "ranked": [
    {
      "option": "<label>",
      "score": <float 0.0-1.0>,
      "pros": ["<short bullet>", ...],
      "cons": ["<short bullet>", ...],
      "risk": "<one-sentence failure mode>"
    },
    ...
  ],
  "rationale": "<one-sentence why>",
  "assumptions": ["<assumption>", ...],
  "unknowns": ["<unknown that would change the answer>", ...],
  "speech": "<one sentence the assistant can read aloud>"
}

Rules:
  1. If the user supplied OPTIONS, rank ALL of them. If they did not,
     propose 2-4 viable options yourself.
  2. Never invent numbers you cannot defend. When uncertain, lower
     ``confidence`` and expand ``unknowns``.
  3. 1 to 4 pros, 1 to 4 cons per option. Short bullets only.
  4. Keep ``speech`` under 180 characters and write it in the SAME
     language as the question.
  5. Return ONLY the JSON object. No markdown fences, no prose.
"""


@dataclass
class DecisionResult:
    recommendation: str
    confidence: float
    ranked: list[dict] = field(default_factory=list)
    rationale: str = ""
    assumptions: list[str] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    speech: str = ""
    source: str = "model"  # "model" | "fallback"
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "recommendation": self.recommendation,
            "confidence":     round(float(self.confidence), 3),
            "ranked":         list(self.ranked),
            "rationale":      self.rationale,
            "assumptions":    list(self.assumptions),
            "unknowns":       list(self.unknowns),
            "speech":         self.speech,
            "source":         self.source,
            "error":          self.error,
        }


def _fallback(question: str,
              options: list[str] | None,
              reason: str) -> DecisionResult:
    """Returned when the model is unreachable. Keeps callers safe (they
    always get a full DecisionResult) and surfaces WHY we couldn't decide
    instead of pretending to."""
    options = options or []
    if options:
        # Rank nothing; copy the list but with 0.5 scores and the error.
        ranked = [{
            "option": o,
            "score":  0.5,
            "pros":   [],
            "cons":   [],
            "risk":   "model unavailable; unranked",
        } for o in options]
        rec = options[0]
    else:
        ranked = []
        rec = ""
    return DecisionResult(
        recommendation=rec,
        confidence=0.0,
        ranked=ranked,
        rationale="Decision model unavailable — returning request as-is.",
        unknowns=[question],
        speech=("I could not reach the decision model right now, so I "
                "cannot recommend one. Try again, or ask me to compare "
                "the options another way."),
        source="fallback",
        error=reason,
    )


def _coerce(raw: Any, question: str,
            options: list[str] | None) -> DecisionResult:
    """Pull the fields we need out of a model response, filling in sane
    defaults for anything missing or malformed."""
    if not isinstance(raw, dict):
        return _fallback(question, options, "model did not return a JSON object")

    def _str(key: str, default: str = "") -> str:
        v = raw.get(key)
        return str(v) if v is not None else default

    def _list(key: str) -> list:
        v = raw.get(key)
        return v if isinstance(v, list) else []

    try:
        conf = float(raw.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    conf = max(0.0, min(1.0, conf))

    ranked = []
    for item in _list("ranked"):
        if not isinstance(item, dict):
            continue
        try:
            score = float(item.get("score", 0.0))
        except (TypeError, ValueError):
            score = 0.0
        score = max(0.0, min(1.0, score))
        ranked.append({
            "option": str(item.get("option", "")).strip(),
            "score":  round(score, 3),
            "pros":   [str(p) for p in (item.get("pros") or []) if p],
            "cons":   [str(c) for c in (item.get("cons") or []) if c],
            "risk":   str(item.get("risk") or ""),
        })

    return DecisionResult(
        recommendation=_str("recommendation"),
        confidence=conf,
        ranked=ranked,
        rationale=_str("rationale"),
        assumptions=[str(a) for a in _list("assumptions") if a],
        unknowns=[str(u) for u in _list("unknowns") if u],
        speech=_str("speech"),
        source="model",
    )


def decide(question: str,
           *,
           options: list[str] | None = None,
           criteria: list[str] | None = None,
           context: str | None = None,
           timeout_ms: int = 15_000) -> dict:
    """Produce a structured recommendation. Returns a dict (see
    ``DecisionResult.to_dict``). Never raises — unreachable model →
    ``source='fallback'`` with an ``error`` field."""
    question = (question or "").strip()
    if not question:
        return _fallback("", options, "empty question").to_dict()

    if _gem is None or not getattr(_gem, "api_key", lambda: "")():
        return _fallback(question, options,
                         "no Gemini key configured").to_dict()

    parts = [f"QUESTION: {question}"]
    if options:
        parts.append("OPTIONS:\n" + "\n".join(f"  - {o}" for o in options))
    if criteria:
        parts.append("CRITERIA:\n" + "\n".join(f"  - {c}" for c in criteria))
    if context:
        parts.append(f"CONTEXT:\n{context[:2000]}")
    user = "\n\n".join(parts)

    try:
        raw = _gem.as_json(
            [_SYSTEM, user],
            tier=getattr(_gem, "SMART", None),
            timeout_ms=timeout_ms,
            default=None,
        )
    except Exception as e:  # pragma: no cover — defensive
        return _fallback(question, options,
                         f"{type(e).__name__}: {e}").to_dict()
    if raw is None:
        return _fallback(question, options,
                         "model returned nothing").to_dict()
    return _coerce(raw, question, options).to_dict()
