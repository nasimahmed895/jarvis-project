"""
Decision action — ranked recommendation for the user.

Thin wrapper around ``core/decide.py``. The planner can call this at any
point the model would otherwise answer "it depends…". Returns a human-
readable summary; the structured JSON lives on the ``result`` field so a
plan step can consume it downstream (see worker.verify specs).
"""
from __future__ import annotations

import json
from typing import Any

from core import decide as _decide


def _split_options(raw: Any) -> list[str] | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    if isinstance(raw, str):
        s = raw.strip()
        # Accept JSON array or newline / | separated list.
        if s.startswith("["):
            try:
                data = json.loads(s)
                if isinstance(data, list):
                    return [str(x).strip() for x in data if str(x).strip()]
            except Exception:
                pass
        if "|" in s:
            return [p.strip() for p in s.split("|") if p.strip()]
        if "\n" in s:
            return [p.strip() for p in s.splitlines() if p.strip()]
        # Fall back to comma-split, but only when ``,`` isn't part of the
        # phrasing of a single option (" in option 1, option 2 " is fine).
        if "," in s:
            return [p.strip() for p in s.split(",") if p.strip()]
    return [str(raw).strip()]


def _split_lines(raw: Any) -> list[str] | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    if isinstance(raw, str):
        if "\n" in raw:
            return [p.strip() for p in raw.splitlines() if p.strip()]
        return [p.strip() for p in raw.split(",") if p.strip()]
    return None


def _fmt(r: dict) -> str:
    lines = [f"Recommendation: {r.get('recommendation', '?')} "
             f"(confidence {r.get('confidence', 0.0):.2f})",
             f"Why: {r.get('rationale', '')}"]
    ranked = r.get("ranked") or []
    for item in ranked[:5]:
        opt = item.get("option", "?")
        score = item.get("score", 0.0)
        lines.append(f"  · {opt} — score {score:.2f}")
        for p in (item.get("pros") or [])[:3]:
            lines.append(f"      + {p}")
        for c in (item.get("cons") or [])[:3]:
            lines.append(f"      - {c}")
        risk = item.get("risk")
        if risk:
            lines.append(f"      risk: {risk}")
    unknowns = r.get("unknowns") or []
    if unknowns:
        lines.append("Unknowns: " + "; ".join(unknowns[:3]))
    if r.get("source") == "fallback" and r.get("error"):
        lines.append(f"(fallback: {r['error']})")
    return "\n".join(lines)


def decide(
    parameters:    dict | None = None,
    response: Any = None,
    player:   Any = None,
    session_memory: Any = None,
) -> str:
    """Rank options for a decision and return a recommendation.

    Fields (all strings accepted):
      question   (required) — the decision to make
      options    JSON array OR newline/comma/|-separated list. Optional —
                 the model proposes options when missing.
      criteria   how to weigh the choice (newline- or comma-separated)
      context    any extra situational context to feed the model
    """
    params = parameters or {}
    question = str(params.get("question") or "").strip()
    if not question:
        return "decide: provide 'question'."
    options = _split_options(params.get("options"))
    criteria = _split_lines(params.get("criteria"))
    context = params.get("context") or None
    result = _decide.decide(
        question,
        options=options,
        criteria=criteria,
        context=str(context) if context else None,
    )
    return _fmt(result)


TOOL = {
    "name": "decide",
    "description": (
        "Rank options for a decision and recommend one, with pros, cons, "
        "risk, confidence, and a one-sentence rationale. Use when the "
        "user asks 'should I…', 'which one…', 'A or B…', or when a plan "
        "step has to choose between branches. Supply 'options' when the "
        "user has them in mind; omit when you want the model to suggest "
        "them. Add 'criteria' (budget, speed, risk, who's affected) and "
        "'context' (deadlines, prior choices) when they are relevant; the "
        "more you give it, the higher-confidence the answer."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "question": {"type": "STRING", "description": "The decision to make."},
            "options":  {"type": "STRING", "description": "Options — JSON array, or lines separated by newline / | / comma. Omit to ask the model to propose options."},
            "criteria": {"type": "STRING", "description": "Weighting criteria (cost, speed, risk, revocability, etc.) — lines or comma-separated."},
            "context":  {"type": "STRING", "description": "Any situational context that should inform the ranking."},
        },
        "required": ["question"],
    },
    "handler": decide,
}
