"""
Research sub-agent.

Takes a topic in plain language, performs 1-3 web searches, fetches the
top results as plain text, and asks a SMART Gemini model for a short
cited summary. Returns markdown the model can read back verbatim or save
to a file.

This is the first specialist on the sub-agent framework — a small
scoped Gemini call with its own role. It does NOT use the main Live
session; everything runs on a background thread through
`core.gemini.text` so the voice loop is never blocked.
"""
from __future__ import annotations

import json
import re
import urllib.request
from typing import Any

from core import gemini as _gem


# Cap single-document size we feed into the summariser so a very long
# page cannot blow the prompt budget. Measured in characters, not tokens —
# a conservative 4 chars/token rule puts the hard limit around 12k tokens.
_PER_DOC_CHARS = 48_000
_TOTAL_DOC_CHARS = 120_000
_MAX_SEARCHES = 3
_MAX_RESULTS_PER_QUERY = 4


_SYSTEM = """\
You are the research specialist of a voice assistant. Given a topic and a
set of plain-text excerpts fetched from the web, produce ONE markdown
report with these sections:

  ## Summary (2–4 sentences)
  ## Key facts
    - <fact 1> (source: <n>)
    - <fact 2> (source: <n>)
    ...
  ## Caveats or uncertainty
    - <short note on what the sources disagreed on, or what you could not confirm>
  ## Sources
    [1] <title> — <url>
    [2] <title> — <url>
    ...

Rules:
  - Use the numeric source tag that appears in each excerpt. Do NOT
    invent sources.
  - If the excerpts contradict each other, say so under Caveats rather
    than silently picking one.
  - If an answer cannot be derived from the excerpts, write "Not found in
    the supplied sources" — never fabricate.
  - Keep the whole report under 600 words. Short is better than complete.
  - Output markdown only, no code fences, no preamble.
"""


def _fetch_text(url: str, timeout: float = 8.0) -> str:
    """Server-side read of a public page. Strips scripts/styles, flattens
    to plain text. Returns '' on error — caller handles."""
    try:
        import requests  # type: ignore
        r = requests.get(
            url, timeout=timeout,
            headers={"User-Agent": "JARVIS-Research/1.0"},
            allow_redirects=True,
        )
        r.raise_for_status()
        html = r.text
    except Exception:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                html = resp.read().decode("utf-8", errors="replace")
        except Exception:
            return ""
    html = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.I)
    html = re.sub(r"<style[\s\S]*?</style>", " ", html, flags=re.I)
    html = re.sub(r"<!--[\s\S]*?-->", " ", html)
    text = re.sub(r"<[^>]+>", " ", html)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
            .replace("&lt;", "<").replace("&gt;", ">")
            .replace("&quot;", '"').replace("&#39;", "'"))
    return re.sub(r"\s+", " ", text).strip()


def _parse_search_result_list(raw: str, max_items: int) -> list[dict]:
    """web_search returns its rendered string; this teases out url + title.
    Not strictly necessary if the tool returns JSON, but web_search is a
    human-facing action, so this is a pragmatic adapter."""
    out: list[dict] = []
    # Bullet lines of the form: "N. Title — URL" or "- Title: URL"
    for line in raw.splitlines():
        m = re.search(r"https?://\S+", line)
        if not m:
            continue
        url = m.group(0).rstrip(").,")
        title = line[:m.start()].strip(" -•*0123456789.").strip(":—") or url
        out.append({"title": title[:120], "url": url})
        if len(out) >= max_items:
            break
    return out


def _run_search(query: str) -> list[dict]:
    """Delegate to the project's existing web_search action."""
    try:
        from actions.web_search import web_search
    except Exception:
        return []
    raw = web_search(parameters={"query": query, "mode": "search"})
    return _parse_search_result_list(raw or "", _MAX_RESULTS_PER_QUERY)


def research(
    parameters:    dict | None = None,
    response: Any = None,
    player:   Any = None,
    session_memory: Any = None,
) -> str:
    """Autonomous research: topic → markdown report. Blocks for the whole
    search+fetch+summarise cycle (typical 10–30 s)."""
    params = parameters or {}
    topic = str(params.get("topic") or params.get("query") or "").strip()
    if not topic:
        return "research: provide a 'topic'."
    try:
        num_queries = int(params.get("num_queries") or 2)
    except (TypeError, ValueError):
        num_queries = 2
    num_queries = max(1, min(_MAX_SEARCHES, num_queries))

    queries = _plan_queries(topic, num_queries)
    sources: list[dict] = []
    seen_urls: set[str] = set()
    for q in queries:
        for hit in _run_search(q):
            u = hit["url"]
            if u in seen_urls:
                continue
            seen_urls.add(u)
            sources.append(hit)
    if not sources:
        return f"research: no web results for {topic!r}."

    # Fetch concurrently-ish: thread pool would help, but keep it simple.
    docs: list[dict] = []
    total = 0
    for i, src in enumerate(sources, start=1):
        text = _fetch_text(src["url"])
        if not text:
            continue
        slice_ = text[:_PER_DOC_CHARS]
        if total + len(slice_) > _TOTAL_DOC_CHARS:
            slice_ = slice_[:max(0, _TOTAL_DOC_CHARS - total)]
            if not slice_:
                break
        docs.append({"n": i, "title": src["title"], "url": src["url"],
                     "text": slice_})
        total += len(slice_)
        if total >= _TOTAL_DOC_CHARS:
            break
    if not docs:
        return f"research: fetched 0 usable pages for {topic!r}."

    # Build the prompt body: topic + numbered excerpts.
    excerpt_block = "\n\n".join(
        f"[{d['n']}] {d['title']} — {d['url']}\n{d['text']}"
        for d in docs
    )
    report = _gem.text(
        [_SYSTEM, f"TOPIC: {topic}\n\nEXCERPTS:\n{excerpt_block}"],
        tier=_gem.SMART, timeout_ms=45_000, default="",
    )
    if not report:
        return f"research: summariser returned nothing for {topic!r}."
    return report.strip()


def _plan_queries(topic: str, n: int) -> list[str]:
    """Ask a FAST model to produce N search queries that cover the topic
    from different angles. Falls back to the raw topic if the model fails."""
    if n <= 1:
        return [topic]
    prompt = (
        "You are the query planner for a research assistant. Given a "
        f"topic, propose {n} distinct short web-search queries that would "
        "together give good coverage — a factual one, a recent-news one, "
        "and a how-or-why one when the topic allows it. Return JSON only: "
        "{\"queries\": [\"q1\", \"q2\", ...]}.\n\n"
        f"TOPIC: {topic}"
    )
    try:
        res = _gem.as_json([prompt], tier=_gem.FAST,
                           timeout_ms=10_000, default=None)
    except Exception:
        res = None
    if isinstance(res, dict) and isinstance(res.get("queries"), list) \
            and res["queries"]:
        return [str(q) for q in res["queries"][:n]]
    return [topic]


TOOL = {
    "name": "research",
    "description": (
        "Autonomous research agent. Runs web searches, fetches the top "
        "pages, and returns a short cited markdown report (Summary / Key "
        "facts / Caveats / Sources). Use when the user asks something "
        "that needs looking up across multiple sources — 'tell me about "
        "X', 'compare A and B', 'what's the latest on Y'. Blocks for "
        "10–30 s; mention you're researching before calling. For a "
        "single quick fact use web_search directly. Report text is "
        "already markdown — speak the Summary aloud and offer the full "
        "report if the user wants it."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "topic":       {"type": "STRING",  "description": "The research topic or question."},
            "num_queries": {"type": "INTEGER", "description": "Search queries to issue (1-3, default 2)."},
        },
        "required": ["topic"],
    },
    "handler": research,
}
