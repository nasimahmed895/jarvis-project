"""
actions/pdf_gen.py — Markdown / plain-text → PDF (P2.3).

Optional dependency. If `reportlab` is not installed the tool returns a
clear install hint rather than failing hard — same pattern as the rest of
the file-processing tools.

Supported input shapes:
    - Plain text / Markdown source passed in `content`.
    - Path to an existing .md / .txt file in `src_path`.

Output: a PDF at `dst_path` (or auto-generated next to `src_path`).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

try:
    from reportlab.lib.pagesizes import LETTER  # type: ignore
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle  # type: ignore
    from reportlab.lib.units import inch  # type: ignore
    from reportlab.platypus import (  # type: ignore
        SimpleDocTemplate, Paragraph, Spacer, PageBreak,
    )
    _HAS_REPORTLAB = True
except Exception:
    _HAS_REPORTLAB = False


_INSTALL_HINT = ("PDF generation requires the `reportlab` package. "
                 "Run `pip install reportlab` and retry.")


# ── Markdown → Platypus flowables (deliberately small) ──────────────────────

def _render_markdown(body: str) -> list:
    if not _HAS_REPORTLAB:
        return []
    styles = getSampleStyleSheet()
    body_style = ParagraphStyle(
        "Body", parent=styles["BodyText"], leading=14, spaceAfter=6,
    )
    code_style = ParagraphStyle(
        "Code", parent=styles["Code"], leading=12,
        backColor="#f2f2f2", borderPadding=4,
    )
    flow: list = []
    buf: list[str] = []

    def flush():
        if not buf:
            return
        para = "<br/>".join(_inline(line) for line in buf)
        flow.append(Paragraph(para, body_style))
        buf.clear()

    for raw in body.splitlines():
        line = raw.rstrip()
        if line.startswith("######"):
            flush(); flow.append(Paragraph(_inline(line.lstrip("# ")), styles["Heading6"]))
        elif line.startswith("#####"):
            flush(); flow.append(Paragraph(_inline(line.lstrip("# ")), styles["Heading5"]))
        elif line.startswith("####"):
            flush(); flow.append(Paragraph(_inline(line.lstrip("# ")), styles["Heading4"]))
        elif line.startswith("###"):
            flush(); flow.append(Paragraph(_inline(line.lstrip("# ")), styles["Heading3"]))
        elif line.startswith("##"):
            flush(); flow.append(Paragraph(_inline(line.lstrip("# ")), styles["Heading2"]))
        elif line.startswith("#"):
            flush(); flow.append(Paragraph(_inline(line.lstrip("# ")), styles["Heading1"]))
        elif line.startswith("```"):
            # Simple fence: emit the enclosed block as a code paragraph.
            flush()
            continue
        elif line.strip() == "---":
            flush(); flow.append(PageBreak())
        elif not line.strip():
            flush(); flow.append(Spacer(1, 6))
        else:
            buf.append(line)
    flush()
    return flow


_INLINE_BOLD = re.compile(r"\*\*(.+?)\*\*")
_INLINE_ITALIC = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")
_INLINE_CODE = re.compile(r"`([^`]+)`")


def _inline(text: str) -> str:
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = _INLINE_BOLD.sub(r"<b>\1</b>", text)
    text = _INLINE_ITALIC.sub(r"<i>\1</i>", text)
    text = _INLINE_CODE.sub(r"<font face='Courier'>\1</font>", text)
    return text


# ── Entry point ─────────────────────────────────────────────────────────────

def generate(content: str, dst_path: str | Path,
             title: str | None = None) -> str:
    """Render `content` to a PDF at `dst_path`. Returns a short status line."""
    if not _HAS_REPORTLAB:
        return _INSTALL_HINT
    dst = Path(dst_path).expanduser().resolve()
    dst.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(
        str(dst),
        pagesize=LETTER,
        title=title or dst.stem,
        leftMargin=0.75 * inch, rightMargin=0.75 * inch,
        topMargin=0.75 * inch, bottomMargin=0.75 * inch,
    )
    flow: list = []
    if title:
        styles = getSampleStyleSheet()
        flow.append(Paragraph(title, styles["Title"]))
        flow.append(Spacer(1, 12))
    flow.extend(_render_markdown(content))
    if not flow:
        # Still render something so we never produce a zero-byte PDF when the
        # user handed us all-whitespace content.
        styles = getSampleStyleSheet()
        flow.append(Paragraph("(empty document)", styles["BodyText"]))
    doc.build(flow)
    size = dst.stat().st_size
    return f"Wrote {dst} ({size} bytes)"


def _tool_entry(parameters: dict, **_kw: Any) -> str:
    """Tool-dispatch adapter. Accepts:
        content    — inline markdown/text, OR
        src_path   — file to read
        dst_path   — output path (optional when src_path set)
        title      — optional document title
    """
    content = parameters.get("content") or ""
    src_path = parameters.get("src_path")
    dst_path = parameters.get("dst_path")
    title = parameters.get("title")

    if src_path:
        src = Path(str(src_path)).expanduser()
        if not src.exists():
            return f"Source not found: {src}"
        content = src.read_text(encoding="utf-8")
        if not dst_path:
            dst_path = src.with_suffix(".pdf")
    if not content.strip():
        return "No content supplied."
    if not dst_path:
        return "dst_path is required when content is inline."
    return generate(content, dst_path, title=title)


TOOL = {
    "name": "pdf_generate",
    "description": (
        "Render markdown or plain-text content to a PDF document. Use for "
        "reports, meeting notes, briefings — anywhere the user wants a "
        "printable file. Supports headings (#..######), **bold**, *italic*, "
        "`inline code`, horizontal-rule page breaks (`---`). Returns the "
        "output path and byte count."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content":  {"type": "string",
                         "description": "Markdown or plain text to render."},
            "src_path": {"type": "string",
                         "description": "Path to a .md / .txt source file (alternative to `content`)."},
            "dst_path": {"type": "string",
                         "description": "Output .pdf path (required when content is inline)."},
            "title":    {"type": "string",
                         "description": "Optional document title shown at top."},
        },
    },
    "run": _tool_entry,
}
