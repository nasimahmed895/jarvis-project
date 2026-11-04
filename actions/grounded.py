"""
Grounded knowledge action — real-world situational awareness.

Thin surface over ``core/grounded.py``. The model should call this
instead of relying on its internal knowledge whenever a fact could be
stale (weather / news / current price / recent event). Every result
carries a ``tag`` so the model can distinguish verified fact from
model-only guess and phrase accordingly.
"""
from __future__ import annotations

import json
from typing import Any

from core import grounded as _g


def _fmt_claim(c: dict) -> str:
    bits = [f"[{c.get('tag','?')} {c.get('confidence',0.0):.2f}] "
            f"{c.get('text','')}"]
    srcs = c.get("sources") or []
    if srcs:
        bits.append("sources: " + ", ".join(s for s in srcs[:4] if s))
    return "  " + "\n  ".join(bits)


def _fmt_claims(label: str, claims: list[dict]) -> str:
    lines = [label + ":"]
    for c in claims:
        lines.append(_fmt_claim(c))
    return "\n".join(lines)


def grounded(
    parameters:    dict | None = None,
    response: Any = None,
    player:   Any = None,
    session_memory: Any = None,
) -> str:
    """Real-world knowledge aggregator: time / location / weather /
    fact-check / situational snapshot. Every output carries a tag:
    verified | estimated | model_knowledge | unknown.

    Actions:
      now          zone=<IANA TZ>
      where        city= (defaults to stored identity.city)
      weather      city=
      fact_check   claim=
      cross_check  claims=<JSON list of strings>
      snapshot     city=     — time + location + weather
    """
    p = parameters or {}
    action = str(p.get("action", "")).lower().strip()

    if action == "now":
        c = _g.time_now(p.get("zone")).to_dict()
        return "Current time:\n" + _fmt_claim(c)

    if action == "where":
        c = _g.location(p.get("city")).to_dict()
        return "Location:\n" + _fmt_claim(c)

    if action == "weather":
        c = _g.weather(p.get("city")).to_dict()
        return "Weather:\n" + _fmt_claim(c)

    if action == "fact_check":
        claim = str(p.get("claim") or "").strip()
        if not claim:
            return "grounded fact_check: provide 'claim'."
        c = _g.fact_check(claim).to_dict()
        return "Fact check:\n" + _fmt_claim(c)

    if action == "cross_check":
        raw = p.get("claims")
        claims: list[str]
        if isinstance(raw, list):
            claims = [str(x).strip() for x in raw if str(x).strip()]
        elif isinstance(raw, str):
            s = raw.strip()
            if s.startswith("["):
                try:
                    data = json.loads(s)
                    claims = [str(x).strip() for x in data if str(x).strip()]
                except Exception:
                    claims = [s]
            else:
                claims = [t.strip() for t in s.splitlines() if t.strip()]
        else:
            claims = []
        if not claims:
            return "grounded cross_check: provide 'claims' as a JSON list."
        results = [c.to_dict() for c in _g.cross_check(claims)]
        return _fmt_claims(f"Cross-check ({len(results)} claims)", results)

    if action == "snapshot":
        snap = _g.situational(p.get("city"))
        return _fmt_claims("Situational snapshot",
                            [snap["time"], snap["location"], snap["weather"]])

    return (f"grounded: unknown action {action!r}. "
            "Actions: now | where | weather | fact_check | "
            "cross_check | snapshot")


TOOL = {
    "name": "grounded",
    "description": (
        "Ground an answer in real-world data. ALWAYS prefer this over "
        "internal knowledge when a fact can go stale: current time, "
        "weather, location, 'is X still true?', 'what did the news "
        "say yesterday about Y?'. Output tags each piece: VERIFIED "
        "(≥2 distinct sources), ESTIMATED (one source), "
        "MODEL_KNOWLEDGE (no external source), UNKNOWN (searched and "
        "found nothing usable). Use 'snapshot' for a fast "
        "time + location + weather triple on proactive ticks. "
        "'fact_check' takes one claim; 'cross_check' takes a list. "
        "Weather + geocoding use Open-Meteo (free, no key)."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "now | where | weather | fact_check | cross_check | snapshot"},
            "zone":   {"type": "STRING",
                       "description": "IANA timezone (for 'now')."},
            "city":   {"type": "STRING",
                       "description": "City name; defaults to stored identity.city."},
            "claim":  {"type": "STRING",
                       "description": "A single claim to fact-check."},
            "claims": {"type": "STRING",
                       "description": "JSON array of claims for 'cross_check'."},
        },
        "required": ["action"],
    },
    "handler": grounded,
}
