"""Report-location resolver.

Picks a writable directory for Markdown + JSON reports. Preference order:

    1. ~/Downloads/JARVIS Reports    (if the OS Downloads folder exists)
    2. ~/Documents/JARVIS Reports    (common secondary location)
    3. The project-local directory configured in `market_config.report_dir`
       (default: `<project>/reports/`)
    4. A tempfile fallback — only if every option above is unwritable

Resolution happens LAZILY at write time, not at import or config load. If
the preferred location becomes unavailable between runs (removable drive,
permissions change), the next report lands at the next writable candidate
and the pipeline records which fallback was used.

No secret or user-identifying path component is ever appended to the
directory name. The fixed "JARVIS Reports" literal is used verbatim.
"""
from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

_SUBDIR = "JARVIS Reports"


@dataclass(frozen=True, slots=True)
class ResolvedLocation:
    path: Path
    source: str          # "downloads" | "documents" | "configured" | "tempfile"
    note: str            # human-readable explanation (shown in the report)


def _probe_writable(d: Path) -> bool:
    """Try to create the directory and write a small probe file. Removes the
    probe on success. Returns True iff the directory accepts writes right now.
    """
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    probe = d / ".jarvis_write_probe"
    try:
        probe.write_text("ok", encoding="utf-8")
    except OSError:
        return False
    try:
        probe.unlink()
    except OSError:
        pass
    return True


def resolve_report_dir(configured: Path | None = None) -> ResolvedLocation:
    """Return the directory where a report should actually be written."""
    candidates: list[tuple[Path, str, str]] = []

    home = Path.home()
    downloads = home / "Downloads"
    if downloads.exists():
        candidates.append(
            (downloads / _SUBDIR, "downloads", f"Saved to your Downloads folder ({downloads / _SUBDIR}).")
        )
    documents = home / "Documents"
    if documents.exists():
        candidates.append(
            (documents / _SUBDIR, "documents", f"Saved to your Documents folder ({documents / _SUBDIR}).")
        )
    if configured is not None:
        candidates.append(
            (Path(configured), "configured", f"Saved to the configured report_dir ({Path(configured)}).")
        )
    for path, source, note in candidates:
        if _probe_writable(path):
            return ResolvedLocation(path=path.resolve(), source=source, note=note)

    # Last-resort tempfile fallback — surfaced in the warnings so the user
    # knows the real location rather than being told the preferred one.
    tmp = Path(tempfile.gettempdir()) / _SUBDIR
    tmp.mkdir(parents=True, exist_ok=True)
    return ResolvedLocation(
        path=tmp.resolve(),
        source="tempfile",
        note=f"Preferred report locations were unwritable; saved to a temp folder ({tmp}).",
    )
