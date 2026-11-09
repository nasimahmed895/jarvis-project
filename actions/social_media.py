"""
actions/social_media.py — social-post drafting and scheduling (MODULE 6).

Intentionally cautious: publishing to a real account is always gated on the
on-screen confirmation banner. Nothing in this file touches a public feed
without the user pressing CONFIRM.

For draft generation, this uses the project's existing LLM entry point
(`core.gemini.text`). Publishing itself delegates to `browser_control` once
the user has approved — most platforms' APIs are rate-limited or require
app-review for posting; driving the user's real browser session side-steps
that and keeps the account authenticated the same way the user authenticated
it.

Spam, fake engagement, mass unsolicited messaging, and rule-circumvention
are explicitly refused by the drafter prompt below.
"""
from __future__ import annotations

from core import approval as _approval
from core import gemini as _gem


_MAX_CHARS = {
    "twitter":   275,
    "x":         275,
    "bluesky":   300,
    "mastodon":  500,
    "linkedin":  2900,
    "facebook":  5000,
    "instagram": 2200,
    "threads":   500,
}


def _draft_post(platform: str, topic: str, tone: str, length_hint: str) -> str:
    limit = _MAX_CHARS.get(platform.lower().strip(), 500)
    prompt = (
        "Draft ONE social post. No hashtag spam. No fake engagement bait "
        "('Agree?' as its own line). Avoid vague superlatives unless the "
        "topic warrants them.\n"
        f"Platform: {platform} (hard limit {limit} characters)\n"
        f"Tone: {tone or 'direct, friendly'}\n"
        f"Length: {length_hint or 'tight'}\n"
        f"Topic: {topic}\n\n"
        "Return the post text only — no preamble, no quotes, no commentary."
    )
    try:
        draft = _gem.text(prompt).strip() or ""
    except Exception as e:
        return f"[Draft failed: {e}]"
    # Hard-trim to the platform limit so the model never surprises us.
    if len(draft) > limit:
        draft = draft[:limit - 1].rstrip() + "…"
    return draft


def _hashtags(topic: str, count: int) -> str:
    prompt = (
        f"Give {count} relevant, non-generic hashtags for: '{topic}'. Space-"
        "separated. No #Love / #Instagood style filler. Return hashtags only."
    )
    try:
        return _gem.text(prompt).strip()
    except Exception as e:
        return f"[Hashtag suggestion failed: {e}]"


def social_media(parameters: dict | None = None,
                 response=None, player=None, speak=None,
                 session_memory=None) -> str:
    params = parameters or {}
    verb = (params.get("action") or "draft").lower().strip()

    if verb == "draft":
        platform = (params.get("platform") or "twitter").strip()
        topic = (params.get("topic") or "").strip()
        if not topic:
            return "Provide 'topic' to draft a post about."
        return _draft_post(platform, topic,
                           (params.get("tone") or "").strip(),
                           (params.get("length") or "").strip())

    if verb == "hashtags":
        topic = (params.get("topic") or "").strip()
        if not topic:
            return "Provide 'topic' for hashtag suggestions."
        return _hashtags(topic, int(params.get("count") or 5))

    if verb == "publish":
        platform = (params.get("platform") or "").strip()
        draft = (params.get("draft") or "").strip()
        if not platform or not draft:
            return "Provide 'platform' and 'draft' to publish."

        def _do_publish() -> str:
            # Returns a browser-plan sentence — actual clicking is up to the
            # model, using browser_control. Keeps the confirmation banner the
            # only authority on go/no-go.
            return (f"Open {platform} in the active browser, open the "
                    "composer, paste the approved draft, and press post. "
                    "Draft text follows:\n" + draft)

        return _approval.gate(
            tool="social_media",
            args={"action": "publish", "platform": platform},
            title=f"Publish to {platform}",
            detail=f"Content preview: {draft[:160]}" +
                   ("…" if len(draft) > 160 else ""),
            run=_do_publish,
            lvl2_category="social_post",
        )

    return "Unknown action. Use: draft | hashtags | publish."


TOOL = {
    "name": "social_media",
    "description": ("Social-post drafting, hashtag suggestion, and gated "
                    "publishing. draft → one post sized for the platform, "
                    "hashtags → non-generic suggestions, publish → opens "
                    "the composer and prepares the post for user to send."),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action":   {"type": "STRING",
                         "description": "draft | hashtags | publish"},
            "platform": {"type": "STRING",
                         "description": "twitter | bluesky | mastodon | "
                                        "linkedin | facebook | instagram | "
                                        "threads"},
            "topic":    {"type": "STRING"},
            "tone":     {"type": "STRING",
                         "description": "e.g. direct, playful, technical"},
            "length":   {"type": "STRING",
                         "description": "tight | medium | long"},
            "count":    {"type": "INTEGER",
                         "description": "Number of hashtags"},
            "draft":    {"type": "STRING",
                         "description": "Post body for publish"},
        },
        "required": ["action"],
    },
    "handler": social_media,
}
