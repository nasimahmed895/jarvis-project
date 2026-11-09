"""
actions/customer_support.py — support-ticket workflow (MODULE 15).

Does not integrate with any particular ticketing system directly — that
glue lives in the per-vendor plugins. This tool handles the parts common
to every support flow: draft a well-shaped ticket, look up a product's
knowledge-base article via web_search, and summarise an existing
conversation thread into an escalation note. Opening a real ticket in a
third-party system is LVL2 (asked once per category) because it is
externally visible but reversible.
"""
from __future__ import annotations

from core import approval as _approval
from core import gemini as _gem

try:
    from actions.web_search import web_search as _web_search  # type: ignore
except Exception:
    _web_search = None


def _draft_ticket(product: str, problem: str,
                  repro_steps: str, severity: str) -> str:
    prompt = (
        "Write a well-structured support ticket. Fields: Summary (one line), "
        "Environment (3 bullets), Steps to reproduce (numbered), Expected vs "
        "Actual, Severity, Attachments-requested. Facts only, no filler.\n"
        f"Product: {product}\nSeverity: {severity}\nProblem:\n{problem}\n\n"
        f"Reproduction steps given by user:\n{repro_steps}"
    )
    try:
        return _gem.text(prompt).strip() or "[Empty reply.]"
    except Exception as e:
        return f"[Ticket drafting failed: {e}]"


def _summarise_thread(thread: str) -> str:
    prompt = (
        "Summarise this support thread for escalation. Produce: (1) one-line "
        "state, (2) chronology bullets, (3) unresolved blockers, (4) next "
        f"step the escalation owner should take.\n\nTHREAD:\n{thread[:5000]}"
    )
    try:
        return _gem.text(prompt).strip() or "[Empty reply.]"
    except Exception as e:
        return f"[Summary failed: {e}]"


def customer_support(parameters: dict | None = None,
                     response=None, player=None, speak=None,
                     session_memory=None) -> str:
    params = parameters or {}
    verb = (params.get("action") or "").lower().strip()

    if verb == "search_kb":
        product = (params.get("product") or "").strip()
        query = (params.get("query") or "").strip()
        if not query:
            return "Provide 'query' for the knowledge-base search."
        q = f"{product} {query} help OR support OR docs".strip()
        if _web_search is None:
            return "Web search unavailable."
        return _web_search({"query": q, "mode": "search",
                            "limit": int(params.get("limit", 8))},
                           player=player)

    if verb == "draft_ticket":
        return _draft_ticket(
            product=(params.get("product") or "unspecified").strip(),
            problem=(params.get("problem") or "").strip(),
            repro_steps=(params.get("repro_steps") or "").strip(),
            severity=(params.get("severity") or "P3").strip(),
        )

    if verb == "summarize_thread":
        thread = (params.get("thread") or "").strip()
        if not thread:
            return "Provide 'thread' text to summarise."
        return _summarise_thread(thread)

    if verb == "open_ticket":
        portal_url = (params.get("portal_url") or "").strip()
        draft = (params.get("draft") or "").strip()
        if not portal_url or not draft:
            return "Provide 'portal_url' and 'draft' to open a ticket."

        def _do_open() -> str:
            return (f"Open {portal_url}, paste the draft, review once, then "
                    "submit. I have prepared the body, user pushes submit.")

        return _approval.gate(
            tool="customer_support",
            args={"action": "open_ticket", "portal_url": portal_url},
            title=f"Open support ticket at {portal_url}",
            detail="Externally visible — ticket will appear in the vendor's "
                   "queue. User must press submit.",
            run=_do_open,
            lvl2_category="form_submit",
        )

    return ("Unknown action. Use: search_kb | draft_ticket | "
            "summarize_thread | open_ticket.")


TOOL = {
    "name": "customer_support",
    "description": ("Support-ticket workflow: knowledge-base search, draft a "
                    "well-shaped ticket from a short problem description, "
                    "summarise an existing thread for escalation, prepare "
                    "(but not auto-submit) a new ticket in a vendor portal."),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action":   {"type": "STRING",
                         "description": "search_kb | draft_ticket | "
                                        "summarize_thread | open_ticket"},
            "product":  {"type": "STRING"},
            "query":    {"type": "STRING",
                         "description": "KB search query"},
            "problem":  {"type": "STRING",
                         "description": "Problem description for draft_ticket"},
            "repro_steps": {"type": "STRING"},
            "severity": {"type": "STRING",
                         "description": "P1 | P2 | P3 | P4"},
            "thread":   {"type": "STRING",
                         "description": "Thread text for summarize_thread"},
            "portal_url": {"type": "STRING"},
            "draft":    {"type": "STRING",
                         "description": "Ticket body to paste into portal"},
            "limit":    {"type": "INTEGER"},
        },
        "required": ["action"],
    },
    "handler": customer_support,
}
