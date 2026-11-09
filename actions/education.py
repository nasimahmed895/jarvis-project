"""
actions/education.py — learning and course discovery (MODULE 14).

Searches major course platforms, academic sources, and scholarship listings;
summarises material; drafts a study plan. Enrolment is gated — nothing paid
or identity-bound happens without the on-screen confirmation.
"""
from __future__ import annotations

from core import approval as _approval
from core import gemini as _gem

try:
    from actions.web_search import web_search as _web_search  # type: ignore
except Exception:
    _web_search = None


_COURSE_SITES = ("coursera.org", "edx.org", "udemy.com", "udacity.com",
                 "khanacademy.org", "futurelearn.com", "classcentral.com")
_PAPER_SITES = ("scholar.google.com", "arxiv.org", "ssrn.com",
                "semanticscholar.org", "jstor.org")
_SCHOLAR_SITES = ("scholarships.com", "fastweb.com", "chevening.org",
                  "daad.de", "fulbrightonline.org")


def _site_filter(sites: tuple[str, ...]) -> str:
    return "site:" + " OR site:".join(sites)


def education(parameters: dict | None = None,
              response=None, player=None, speak=None,
              session_memory=None) -> str:
    params = parameters or {}
    verb = (params.get("action") or "search_course").lower().strip()
    topic = (params.get("topic") or "").strip()

    if verb == "search_course":
        if _web_search is None:
            return "Web search unavailable."
        if not topic:
            return "Provide 'topic' to search courses for."
        q = f"{topic} course {_site_filter(_COURSE_SITES)}"
        return _web_search({"query": q, "mode": "search",
                            "limit": int(params.get("limit", 10))},
                           player=player)

    if verb == "search_paper":
        if not topic:
            return "Provide 'topic' to search papers for."
        q = f"{topic} {_site_filter(_PAPER_SITES)}"
        return _web_search({"query": q, "mode": "research",
                            "limit": int(params.get("limit", 10))},
                           player=player)

    if verb == "search_scholarship":
        q = f"{topic or 'scholarship'} {_site_filter(_SCHOLAR_SITES)}"
        return _web_search({"query": q, "mode": "search",
                            "limit": int(params.get("limit", 10))},
                           player=player)

    if verb == "study_plan":
        goal = (params.get("goal") or "").strip() or topic
        weeks = int(params.get("weeks") or 4)
        hours_per_week = int(params.get("hours_per_week") or 5)
        if not goal:
            return "Provide 'goal' (or 'topic') for the study plan."
        prompt = (
            f"Create a {weeks}-week study plan for: '{goal}'. The learner has "
            f"{hours_per_week} hours per week. For each week give: focus "
            "topic, one primary resource (course, book, or paper), one "
            "practice exercise, one verification question. Markdown table."
        )
        try:
            return _gem.text(prompt).strip() or "[Empty reply.]"
        except Exception as e:
            return f"[Study plan failed: {e}]"

    if verb == "summarize":
        material = (params.get("material") or "").strip()
        if not material:
            return "Provide 'material' text to summarise."
        prompt = ("Summarise the following learning material into: 5 key "
                  "concepts, 3 worked examples (brief), and 3 revision "
                  f"questions. Material:\n{material[:4000]}")
        try:
            return _gem.text(prompt).strip() or "[Empty reply.]"
        except Exception as e:
            return f"[Summary failed: {e}]"

    if verb == "enroll":
        url = (params.get("url") or "").strip()
        if not url:
            return "Provide 'url' of the course to enrol in."

        def _do_enroll() -> str:
            return (f"Open {url} and prepare the enrolment form. The user "
                    "must confirm any payment or identity step themselves.")

        return _approval.gate(
            tool="education", args={"action": "enroll", "url": url},
            title=f"Prepare enrolment at {url}",
            detail="I will open the course page and fill known fields. The "
                   "user must press enrol / pay themselves.",
            run=_do_enroll,
        )

    return ("Unknown action. Use: search_course | search_paper | "
            "search_scholarship | study_plan | summarize | enroll.")


TOOL = {
    "name": "education",
    "description": ("Online learning discovery and planning. Searches "
                    "Coursera/edX/Udemy/etc. for courses, Google Scholar/"
                    "arXiv for papers, scholarship directories, and drafts "
                    "multi-week study plans. Enrolment is gated."),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action":   {"type": "STRING",
                         "description": "search_course | search_paper | "
                                        "search_scholarship | study_plan | "
                                        "summarize | enroll"},
            "topic":    {"type": "STRING"},
            "goal":     {"type": "STRING",
                         "description": "Learning goal for study_plan"},
            "weeks":    {"type": "INTEGER"},
            "hours_per_week": {"type": "INTEGER"},
            "limit":    {"type": "INTEGER"},
            "material": {"type": "STRING",
                         "description": "Text to summarise"},
            "url":      {"type": "STRING",
                         "description": "Course URL for enroll"},
        },
        "required": ["action"],
    },
    "handler": education,
}
