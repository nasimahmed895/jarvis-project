"""
actions/real_estate.py — property search and comparison (MODULE 13).

Delegates discovery to web_search and page reading to browser_control; adds
a domain-shaped query builder and a comparison-report helper. Inquiries and
viewing requests go through the approval gate.
"""
from __future__ import annotations

from core import approval as _approval
from core import gemini as _gem

try:
    from actions.web_search import web_search as _web_search  # type: ignore
except Exception:
    _web_search = None


_SITES = ("zillow.com", "realtor.com", "redfin.com", "trulia.com",
          "rightmove.co.uk", "zoopla.co.uk", "idealista.com")


def _build_query(params: dict) -> str:
    bits: list[str] = []
    kind = (params.get("kind") or "house").strip()
    bits.append(kind)
    location = (params.get("location") or "").strip()
    if location:
        bits.append(f"in {location}")
    bedrooms = params.get("bedrooms")
    if bedrooms:
        bits.append(f"{bedrooms} bed")
    budget_max = params.get("budget_max")
    if budget_max:
        bits.append(f"under ${budget_max}")
    for_rent = bool(params.get("for_rent", False))
    bits.append("for rent" if for_rent else "for sale")
    bits.append("site:" + " OR site:".join(_SITES))
    return " ".join(bits)


def real_estate(parameters: dict | None = None,
                response=None, player=None, speak=None,
                session_memory=None) -> str:
    params = parameters or {}
    verb = (params.get("action") or "search").lower().strip()

    if verb == "search":
        if _web_search is None:
            return "Web search unavailable."
        q = _build_query(params)
        if player:
            player.write_log(f"[RealEstate] {q}")
        return _web_search({"query": q, "mode": "search",
                            "limit": int(params.get("limit", 10))},
                           player=player)

    if verb == "compare":
        items = params.get("items") or []
        if not isinstance(items, list) or not items:
            return "Provide 'items' as a list of property descriptions/URLs."
        return _web_search({"mode": "compare", "items": items,
                            "aspect": params.get("aspect", "value")},
                           player=player)

    if verb == "summarize":
        listing = (params.get("listing") or "").strip()
        if not listing:
            return "Provide 'listing' text to summarise."
        prompt = ("Summarise this property listing in 5 bullets: price, "
                  "bedrooms/baths, area, standout features, and one honest "
                  f"caveat. Listing:\n{listing[:3000]}")
        try:
            return _gem.text(prompt).strip() or "[Empty reply.]"
        except Exception as e:
            return f"[Summary failed: {e}]"

    if verb == "inquire":
        # Sending an inquiry to a real agent is LVL2 — ask once per session.
        url = (params.get("url") or "").strip()
        note = (params.get("note") or "").strip()
        if not url:
            return "Provide 'url' of the listing."

        def _do_inquire() -> str:
            return (f"Open {url} and prepare an inquiry with the note: "
                    f"\"{note or '[none provided]'}\". User must submit.")

        return _approval.gate(
            tool="real_estate", args={"action": "inquire", "url": url},
            title=f"Prepare inquiry to {url}",
            detail=("I will draft and fill the contact form, but the user "
                    "must press send."),
            run=_do_inquire,
            lvl2_category="form_submit",
        )

    return "Unknown action. Use: search | compare | summarize | inquire."


TOOL = {
    "name": "real_estate",
    "description": ("Property search and comparison across the major real "
                    "estate portals. search → listings, compare → side-by-"
                    "side value scoring, summarize → one-paragraph brief, "
                    "inquire → drafts a contact message (user sends)."),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action":    {"type": "STRING",
                          "description": "search | compare | summarize | inquire"},
            "kind":      {"type": "STRING",
                          "description": "house | apartment | condo | studio"},
            "location":  {"type": "STRING"},
            "bedrooms":  {"type": "INTEGER"},
            "budget_max": {"type": "INTEGER"},
            "for_rent":  {"type": "BOOLEAN"},
            "limit":     {"type": "INTEGER"},
            "items":     {"type": "ARRAY", "items": {"type": "STRING"},
                          "description": "URLs or descriptions to compare"},
            "aspect":    {"type": "STRING",
                          "description": "Dimension to compare on"},
            "listing":   {"type": "STRING",
                          "description": "Listing text for summarize"},
            "url":       {"type": "STRING",
                          "description": "Listing URL for inquire"},
            "note":      {"type": "STRING",
                          "description": "Message for inquire"},
        },
        "required": ["action"],
    },
    "handler": real_estate,
}
