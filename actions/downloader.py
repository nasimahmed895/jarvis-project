"""
actions/downloader.py — URL → local file (P3.6).

Scope: fetch a URL to the user's JARVIS uploads directory. Covers the common
case where the user says "download that PDF" / "save this image" after the
browser has surfaced a direct URL. Browser-session-bound downloads (SPA
blobs, authenticated endpoints) remain in `actions/browser_control.py` —
this module does not touch Playwright.

Safety:
- Only http(s) schemes accepted.
- Default size cap (`max_mb`) stops a runaway download from filling disk.
- Returns informative strings; errors are never raised to the tool caller.

Output path: `<UPLOADS_DIR>/<safe filename>`. If the file already exists a
numeric suffix is appended so no existing file is silently overwritten.
"""
from __future__ import annotations

import os
import re
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


_DEFAULT_MAX_MB = 500
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36 JARVIS/1.0"
)


def _uploads_dir() -> Path:
    """Reuse the dashboard's resolver so downloads land in the same folder
    as user-uploaded files. Falls back to ~/Downloads/JARVIS Uploads when
    the dashboard module is unavailable (e.g. headless action pass)."""
    try:
        from dashboard.server import UPLOADS_DIR   # type: ignore
        return Path(UPLOADS_DIR)
    except Exception:
        d = Path.home() / "Downloads" / "JARVIS Uploads"
        d.mkdir(parents=True, exist_ok=True)
        return d


def _safe_filename(url: str, hint: str | None = None) -> str:
    raw = hint or urllib.parse.unquote(
        Path(urllib.parse.urlparse(url).path).name
    )
    raw = raw.strip() or "download"
    name = _SAFE_NAME.sub("_", raw).strip("._") or "download"
    return name


def _unique_path(dst_dir: Path, name: str) -> Path:
    target = dst_dir / name
    if not target.exists():
        return target
    stem, _, ext = name.rpartition(".")
    if not stem:
        stem, ext = name, ""
    i = 2
    while True:
        candidate_name = f"{stem}_{i}.{ext}" if ext else f"{stem}_{i}"
        candidate = dst_dir / candidate_name
        if not candidate.exists():
            return candidate
        i += 1


def download(url: str, *, filename: str | None = None,
             dst_dir: str | Path | None = None,
             max_mb: int = _DEFAULT_MAX_MB,
             opener: urllib.request.OpenerDirector | None = None) -> dict:
    """Fetch `url` and return a dict `{ok, path, bytes, content_type}`.
    Raises `ValueError` on scheme violations before any network call."""
    if not url or not isinstance(url, str):
        raise ValueError("url is required")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"scheme {parsed.scheme!r} not allowed")

    dst_root = Path(dst_dir).expanduser() if dst_dir else _uploads_dir()
    dst_root.mkdir(parents=True, exist_ok=True)
    name = _safe_filename(url, hint=filename)
    target = _unique_path(dst_root, name)
    cap = int(max_mb) * 1024 * 1024

    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    open_fn = opener.open if opener is not None else urllib.request.urlopen
    with open_fn(req, timeout=30) as resp, open(target, "wb") as out:
        content_type = resp.headers.get("Content-Type") if hasattr(resp, "headers") else None
        bytes_written = 0
        while True:
            chunk = resp.read(1024 * 64)
            if not chunk:
                break
            bytes_written += len(chunk)
            if bytes_written > cap:
                out.close()
                try:
                    target.unlink()
                except OSError:
                    pass
                raise RuntimeError(f"download exceeded {max_mb} MB cap")
            out.write(chunk)
    return {
        "ok": True,
        "path": str(target),
        "bytes": bytes_written,
        "content_type": content_type,
    }


def _tool_entry(parameters: dict, **_kw: Any) -> str:
    url = parameters.get("url")
    if not url:
        return "Parameter `url` is required."
    try:
        info = download(
            url,
            filename=parameters.get("filename"),
            dst_dir=parameters.get("dst_dir"),
            max_mb=int(parameters.get("max_mb", _DEFAULT_MAX_MB)),
        )
    except ValueError as e:
        return f"Refused: {e}"
    except Exception as e:
        return f"Download failed: {type(e).__name__}: {e}"
    return f"Downloaded {info['bytes']} bytes → {info['path']}"


TOOL = {
    "name": "download",
    "description": (
        "Fetch a URL to local disk under the JARVIS uploads directory. "
        "http / https only. Enforces a size cap (default 500 MB) and never "
        "overwrites existing files. Use for direct-link downloads — "
        "browser_control still owns authenticated browser sessions."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url":      {"type": "string"},
            "filename": {"type": "string",
                         "description": "Optional override for the saved name."},
            "dst_dir":  {"type": "string",
                         "description": "Override destination directory."},
            "max_mb":   {"type": "integer",
                         "description": "Reject downloads larger than this (default 500)."},
        },
        "required": ["url"],
    },
    "run": _tool_entry,
}
