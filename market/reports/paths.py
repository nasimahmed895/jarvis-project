"""Safe report filenames and directory handling."""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ..exceptions import MarketError

_SAFE = re.compile(r"[^a-zA-Z0-9_\-\.]")


def _slug(part: str) -> str:
    s = _SAFE.sub("_", part).strip("_.")
    return s[:64] or "report"


def report_paths(
    base_dir: Path, asset: str, kind: str = "analysis", now: datetime | None = None
) -> tuple[Path, Path, str]:
    """Return (md_path, json_path, report_id).

    Each report gets a unique ID that is embedded in both the Markdown and
    the JSON file so the two can be matched again after the fact.
    """
    base_dir = Path(base_dir)
    base_dir.mkdir(parents=True, exist_ok=True)
    ts = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    short_id = uuid.uuid4().hex[:10]
    report_id = f"{_slug(kind)}-{_slug(asset)}-{ts}-{short_id}"
    md = base_dir / f"{report_id}.md"
    js = base_dir / f"{report_id}.json"
    # Guard: paths must stay inside base_dir (no traversal via asset name).
    base_resolved = base_dir.resolve()
    for p in (md, js):
        if base_resolved not in p.resolve().parents:
            raise MarketError(f"report path escaped base: {p}")
    return md, js, report_id
