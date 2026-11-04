"""
Report / presentation / memo generator.

Takes a topic + a format (executive summary, briefing memo, status report,
presentation outline) and produces a structured markdown document. The
heavy lifting goes to Gemini SMART through ``gemini.as_json``; a plain
skeleton falls out when no model is reachable so the planner still gets a
valid document back.

Optionally writes the output to disk under the user's home directory
(sandboxed, like the rest of the file-writing actions). Never emails,
never uploads.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
from pathlib import Path
from typing import Any

try:
    from core import gemini as _gem  # type: ignore
except Exception:  # pragma: no cover
    _gem = None  # type: ignore[assignment]


# Format → required sections. The planner can override by passing
# ``sections`` explicitly, but the defaults are what makes each format
# recognisable.
_FORMATS: dict[str, list[str]] = {
    "executive_summary": [
        "Headline",
        "Decision Requested",
        "Context",
        "Options Considered",
        "Recommendation",
        "Risks",
        "Next Steps",
    ],
    "briefing_memo": [
        "Subject",
        "Background",
        "Key Facts",
        "Analysis",
        "Recommendation",
        "Open Questions",
    ],
    "status_report": [
        "Progress This Period",
        "On Track",
        "At Risk",
        "Blockers",
        "Decisions Needed",
        "Next Period",
    ],
    "presentation_outline": [
        "Title Slide",
        "Problem",
        "Why Now",
        "Approach",
        "Evidence",
        "Ask",
        "Appendix",
    ],
    "meeting_notes": [
        "Participants",
        "Decisions",
        "Action Items",
        "Discussion Highlights",
        "Next Meeting",
    ],
}


_SYSTEM = """\
You are the report-writing brain of a voice assistant.

Produce a single JSON object with this shape:

{
  "title": "<document title>",
  "subtitle": "<optional single-line context, OR ''>",
  "sections": [
    {
      "heading": "<section heading, from the provided list>",
      "paragraphs": ["<paragraph>", "..."],    // may be empty
      "bullets":    ["<bullet>", "..."]        // may be empty
    },
    ...
  ]
}

Rules:
  1. Use EXACTLY the section headings supplied under REQUIRED SECTIONS,
     in the order given. Do not invent or drop sections.
  2. Short, executive-ready prose. One paragraph per section unless the
     format calls for structured output (bullets).
  3. If a section has no content available, include it with an empty
     ``paragraphs`` and a single bullet "No material yet." — never omit.
  4. Keep the whole document under 900 words.
  5. Return ONLY the JSON object. No markdown fences, no commentary.
"""


def _skeleton(title: str, sections: list[str], note: str | None = None) -> dict:
    """Minimal structured skeleton used when the model is unreachable.
    Lets callers always see the format they asked for, even on offline."""
    return {
        "title": title,
        "subtitle": "(model unavailable — skeleton only)",
        "sections": [{
            "heading": s,
            "paragraphs": [],
            "bullets": [note] if note else ["No material yet."],
        } for s in sections],
    }


def _call_model(topic: str, sections: list[str],
                context: str | None, audience: str | None,
                tone: str | None) -> dict:
    if _gem is None or not getattr(_gem, "api_key", lambda: "")():
        return _skeleton(topic, sections, note="No Gemini key configured.")
    parts = [
        f"TOPIC: {topic}",
        "REQUIRED SECTIONS:\n" + "\n".join(f"  - {s}" for s in sections),
    ]
    if audience:
        parts.append(f"AUDIENCE: {audience}")
    if tone:
        parts.append(f"TONE: {tone}")
    if context:
        parts.append(f"CONTEXT:\n{context[:4000]}")
    user = "\n\n".join(parts)
    try:
        raw = _gem.as_json(
            [_SYSTEM, user],
            tier=getattr(_gem, "SMART", None),
            timeout_ms=45_000,
            default=None,
        )
    except Exception as e:
        return _skeleton(topic, sections,
                         note=f"model error: {type(e).__name__}")
    if not isinstance(raw, dict):
        return _skeleton(topic, sections, note="model returned nothing")
    # Normalise.
    title = str(raw.get("title") or topic)
    subtitle = str(raw.get("subtitle") or "")
    secs_in = raw.get("sections") or []
    by_heading = {}
    if isinstance(secs_in, list):
        for s in secs_in:
            if isinstance(s, dict):
                h = str(s.get("heading") or "").strip()
                if h:
                    by_heading[h] = {
                        "heading": h,
                        "paragraphs": [str(p) for p in (s.get("paragraphs") or []) if p],
                        "bullets":    [str(b) for b in (s.get("bullets") or []) if b],
                    }
    rebuilt = []
    for s in sections:
        if s in by_heading:
            rebuilt.append(by_heading[s])
        else:
            rebuilt.append({
                "heading": s, "paragraphs": [],
                "bullets": ["No material yet."],
            })
    return {"title": title, "subtitle": subtitle, "sections": rebuilt}


def _render_markdown(doc: dict) -> str:
    lines = [f"# {doc.get('title', '(untitled)')}"]
    if doc.get("subtitle"):
        lines.append(f"_{doc['subtitle']}_")
    for s in doc.get("sections", []):
        lines.append("")
        lines.append(f"## {s.get('heading','?')}")
        for p in s.get("paragraphs", []):
            lines.append(p.strip())
        for b in s.get("bullets", []):
            lines.append(f"- {b.strip()}")
    return "\n".join(lines).strip() + "\n"


_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9._ -]+")


def _safe_filename(topic: str, suffix: str = "md") -> str:
    base = _FILENAME_SAFE.sub("", topic).strip() or "report"
    base = re.sub(r"\s+", "_", base)[:80]
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M")
    return f"{stamp}_{base}.{suffix}"


def _safe_output_path(raw_path: str | None, topic: str,
                      suffix: str = "md") -> Path:
    """Resolve an output path to something under the user's home so a
    hostile plan can't write into /etc. Defaults to ~/Documents/JARVIS Reports/."""
    home = Path.home()
    default_dir = home / "Documents" / "JARVIS Reports"
    if raw_path:
        p = Path(raw_path).expanduser()
        if not p.is_absolute():
            p = home / p
        try:
            p = p.resolve()
            p.relative_to(home.resolve())
        except Exception:
            p = default_dir / p.name
    else:
        p = default_dir / _safe_filename(topic, suffix)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _write_docx(path: Path, doc: dict) -> bool:
    """Write a Word document when python-docx is installed."""
    try:
        from docx import Document
    except Exception as e:
        print(f"[ReportGen] python-docx unavailable: {e}")
        return False
    d = Document()
    d.add_heading(doc.get("title", "Report"), level=0)
    if doc.get("subtitle"):
        d.add_paragraph(doc["subtitle"])
    for s in doc.get("sections", []):
        d.add_heading(s.get("heading", "?"), level=1)
        for p in s.get("paragraphs", []):
            d.add_paragraph(p)
        for b in s.get("bullets", []):
            d.add_paragraph(b, style="List Bullet")
    d.save(str(path))
    return True


def report_gen(
    parameters:    dict | None = None,
    response: Any = None,
    player:   Any = None,
    session_memory: Any = None,
) -> str:
    """Generate a structured document (markdown or docx).

    Fields:
      topic      (required) — subject of the document
      format     executive_summary | briefing_memo | status_report |
                 presentation_outline | meeting_notes
                 (default: executive_summary)
      sections   override the default section list for the chosen format;
                 JSON array OR newline/comma/|-separated
      audience   who's reading it (board, team, investor, customer)
      tone       formal | neutral | plain
      context    any supporting facts to feed the model
      save_as    'md' (default) | 'docx' | 'none'
      path       optional output path (resolved under $HOME)
    """
    p = parameters or {}
    topic = str(p.get("topic") or "").strip()
    if not topic:
        return "report_gen: provide 'topic'."

    fmt = str(p.get("format") or "executive_summary").lower()
    default_sections = _FORMATS.get(fmt)
    if default_sections is None:
        return (f"report_gen: unknown format {fmt!r}. "
                f"Formats: {', '.join(_FORMATS)}.")

    raw_sections = p.get("sections")
    if isinstance(raw_sections, list):
        sections = [str(x).strip() for x in raw_sections if str(x).strip()]
    elif isinstance(raw_sections, str) and raw_sections.strip():
        s = raw_sections.strip()
        if s.startswith("["):
            try:
                data = json.loads(s)
                if isinstance(data, list):
                    sections = [str(x).strip() for x in data if str(x).strip()]
                else:
                    sections = default_sections
            except Exception:
                sections = default_sections
        else:
            for sep in ("\n", "|", ","):
                if sep in s:
                    sections = [t.strip() for t in s.split(sep) if t.strip()]
                    break
            else:
                sections = [s]
    else:
        sections = default_sections

    doc = _call_model(
        topic, sections,
        context=p.get("context"),
        audience=p.get("audience"),
        tone=p.get("tone"),
    )
    md = _render_markdown(doc)

    save_as = str(p.get("save_as") or "md").lower()
    if save_as == "none":
        # Preserve the markdown in the return value so the planner can
        # route it anywhere else it likes.
        return md

    try:
        if save_as == "docx":
            out_path = _safe_output_path(p.get("path"), topic, "docx")
            if _write_docx(out_path, doc):
                return f"Wrote {out_path}\n\n{md}"
            # Fallback to markdown if docx failed.
            md_path = _safe_output_path(None, topic, "md")
            md_path.write_text(md, encoding="utf-8")
            return (f"python-docx unavailable; wrote markdown instead: "
                    f"{md_path}\n\n{md}")
        # Default: write markdown.
        out_path = _safe_output_path(p.get("path"), topic, "md")
        out_path.write_text(md, encoding="utf-8")
        return f"Wrote {out_path}\n\n{md}"
    except Exception as e:
        return (f"report_gen: write failed ({type(e).__name__}: {e}). "
                f"Document:\n\n{md}")


TOOL = {
    "name": "report_gen",
    "description": (
        "Produce a structured report, briefing memo, status report, "
        "presentation outline, or meeting-notes document. Pass 'topic' "
        "(required) and 'format' (executive_summary | briefing_memo | "
        "status_report | presentation_outline | meeting_notes). The "
        "planner may override 'sections'. Writes to "
        "~/Documents/JARVIS Reports/ by default; pass save_as='none' to "
        "get just the markdown back, or save_as='docx' for a Word file "
        "(requires python-docx). Content goes through Gemini when a key "
        "is configured; otherwise a labelled skeleton is returned."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "topic":    {"type": "STRING",  "description": "Document subject."},
            "format":   {"type": "STRING",  "description": "executive_summary | briefing_memo | status_report | presentation_outline | meeting_notes"},
            "sections": {"type": "STRING",  "description": "Override default sections (JSON array or |/newline/comma separated)."},
            "audience": {"type": "STRING",  "description": "Reader: board, team, investor, customer…"},
            "tone":     {"type": "STRING",  "description": "formal | neutral | plain"},
            "context":  {"type": "STRING",  "description": "Facts / background to feed the model."},
            "save_as":  {"type": "STRING",  "description": "md (default) | docx | none"},
            "path":     {"type": "STRING",  "description": "Optional output path (resolved under $HOME)."},
        },
        "required": ["topic"],
    },
    "handler": report_gen,
}
