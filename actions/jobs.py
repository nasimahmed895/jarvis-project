"""
actions/jobs.py — job-hunting and career automation (MODULE 8).

Thin orchestrator on top of the existing primitives:
  * actions/web_search.py     → find listings across sites
  * actions/browser_control.py → open, read, interact with a specific listing
  * actions/report_gen.py      → compile comparison reports
  * core/approval.py           → application submission is LVL3 (never auto)

What this module does itself:
  - Build a well-formed job-search query from structured params.
  - Score results against a stored resume profile (config/resume.json, if any).
  - Draft a cover letter per listing (LLM call via core/gemini).
  - Never submits an application — submission is deliberately parked behind
    the on-screen confirmation banner.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core import approval as _approval
from core import gemini as _gem

try:
    from actions.web_search import web_search as _web_search  # type: ignore
except Exception:
    _web_search = None


_BASE_DIR = Path(__file__).resolve().parent.parent
_RESUME_PATH = _BASE_DIR / "config" / "resume.json"


def _load_resume() -> dict:
    try:
        return json.loads(_RESUME_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _build_query(params: dict) -> str:
    bits: list[str] = []
    title = (params.get("title") or "").strip()
    if title:
        bits.append(title)
    location = (params.get("location") or "").strip()
    if location:
        bits.append(location)
    if bool(params.get("remote", False)):
        bits.append("remote")
    salary_min = params.get("salary_min")
    if salary_min:
        bits.append(f"${salary_min}+")
    experience = (params.get("experience") or "").strip()
    if experience:
        bits.append(experience)
    boards = params.get("boards") or ["linkedin.com/jobs", "indeed.com",
                                      "weworkremotely.com", "remote.co"]
    if isinstance(boards, list) and boards:
        bits.append("site:" + " OR site:".join(str(b) for b in boards))
    return " ".join(bits) or "software engineer jobs"


def _draft_cover_letter(listing: str, resume: dict) -> str:
    """One LLM call — no streaming, no tools. Keeps the loop simple."""
    name = (resume.get("name") or "the candidate")
    skills = ", ".join(resume.get("skills", [])[:10]) or "the required skills"
    prompt = (
        "Write a concise 150-word cover letter for the job listing below. "
        "The candidate is "
        f"{name}. Highlight these skills: {skills}. No boilerplate openers "
        "('I am writing to apply'). First-person, specific to the listing.\n\n"
        f"LISTING:\n{listing[:2500]}"
    )
    try:
        return _gem.text(prompt).strip() or "[Empty reply from model.]"
    except Exception as e:
        return f"[Could not draft cover letter: {e}]"


def jobs(parameters: dict | None = None,
         response=None, player=None, speak=None, session_memory=None) -> str:
    params = parameters or {}
    verb = (params.get("action") or "search").lower().strip()

    if verb == "search":
        if _web_search is None:
            return "Web search unavailable."
        q = _build_query(params)
        if player:
            player.write_log(f"[Jobs] Searching: {q}")
        return _web_search({"query": q, "mode": "search",
                            "limit": int(params.get("limit", 10))},
                           player=player)

    if verb == "draft_letter":
        listing = (params.get("listing") or "").strip()
        if not listing:
            return "Provide 'listing' text to draft a letter from."
        return _draft_cover_letter(listing, _load_resume())

    if verb == "match":
        # Score a listing against the resume — pure LLM call.
        resume = _load_resume()
        if not resume:
            return (f"No resume found at {_RESUME_PATH}. Save one with "
                    "action='save_resume' (keys: name, skills, experience).")
        listing = (params.get("listing") or "").strip()
        if not listing:
            return "Provide 'listing' text to score."
        prompt = (
            "On a 0-100 scale, rate how well this candidate matches the "
            "listing. Give the number, then one sentence why. Candidate: "
            f"{json.dumps(resume)[:1200]}\nListing:\n{listing[:2500]}"
        )
        try:
            return _gem.text(prompt).strip() or "[Empty reply from model.]"
        except Exception as e:
            return f"[Match scoring failed: {e}]"

    if verb == "save_resume":
        data = params.get("resume")
        if not isinstance(data, dict):
            return "Provide 'resume' as a JSON object with name/skills/experience."
        _RESUME_PATH.parent.mkdir(parents=True, exist_ok=True)
        _RESUME_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return f"Resume saved at {_RESUME_PATH}."

    if verb == "apply":
        # Intentionally gated: filing a real application is LVL3.
        listing_url = (params.get("url") or "").strip()
        if not listing_url:
            return "Provide 'url' of the listing to apply to."

        def _do_apply() -> str:
            return (f"Open {listing_url} and prepare the application in the "
                    "browser; the user must complete submission themselves.")

        return _approval.gate(
            tool="jobs", args={"action": "apply", "url": listing_url},
            title=f"Apply to {listing_url}",
            detail=("I will open the listing and fill the known fields, but I "
                    "will NOT submit — the user must press submit."),
            run=_do_apply,
        )

    return ("Unknown action. Use: search | draft_letter | match | "
            "save_resume | apply.")


TOOL = {
    "name": "jobs",
    "description": ("Job-hunting workflow: search listings, score against the "
                    "stored resume, draft tailored cover letters, prepare "
                    "(but never silently submit) applications. Submission is "
                    "always gated on the on-screen confirmation."),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "search | draft_letter | match | "
                                      "save_resume | apply"},
            "title":     {"type": "STRING", "description": "Job title"},
            "location":  {"type": "STRING", "description": "City or country"},
            "remote":    {"type": "BOOLEAN", "description": "Only remote roles"},
            "salary_min": {"type": "INTEGER", "description": "Minimum salary"},
            "experience": {"type": "STRING",
                           "description": "junior | mid | senior | lead"},
            "boards":    {"type": "ARRAY", "items": {"type": "STRING"},
                          "description": "Job boards to restrict to"},
            "limit":     {"type": "INTEGER", "description": "Max listings"},
            "listing":   {"type": "STRING",
                          "description": "Listing text for draft_letter/match"},
            "url":       {"type": "STRING",
                          "description": "Listing URL for apply"},
            "resume":    {"type": "OBJECT",
                          "description": "Resume payload for save_resume",
                          "properties": {}},
        },
        "required": ["action"],
    },
    "handler": jobs,
}
