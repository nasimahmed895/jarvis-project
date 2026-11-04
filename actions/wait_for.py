"""
Polling-wait primitive for the agent.

Lets the planner insert a step that blocks until a condition holds, so a
multi-step plan can survive slow-loading pages, backgrounded installs,
downloads that take a while to land, or a file the user is about to drop.

Every mode is bounded — no infinite waits. The tool reports whether the
condition landed or the deadline expired, and the worker's verifier decides
whether that counts as a step pass or a step fail.
"""
from __future__ import annotations

import os
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any


_DEFAULT_TIMEOUT = 30.0      # seconds
_MAX_TIMEOUT     = 600.0     # ten minutes is the ceiling
_POLL_SECONDS    = 0.5


def _clip_timeout(raw) -> float:
    try:
        v = float(raw) if raw is not None else _DEFAULT_TIMEOUT
    except (TypeError, ValueError):
        v = _DEFAULT_TIMEOUT
    return max(_POLL_SECONDS, min(_MAX_TIMEOUT, v))


def _browser_url() -> str | None:
    """Current active URL via the macOS AppleScript bridge, None if unavailable."""
    try:
        from actions import _mac_chrome as _mc
        if not _mc.available() or not _mc.is_running("chrome"):
            return None
        return _mc.active_url("chrome")
    except Exception:
        return None


def _browser_text() -> str:
    try:
        from actions import _mac_chrome as _mc
        if not _mc.available() or not _mc.is_running("chrome"):
            return ""
        return _mc.get_text("chrome", limit=20000)
    except Exception:
        return ""


def _http_ok(url: str) -> bool:
    try:
        req = urllib.request.Request(url, method="HEAD",
                                      headers={"User-Agent": "JARVIS-wait/1.0"})
        with urllib.request.urlopen(req, timeout=4) as r:
            return 200 <= r.status < 400
    except Exception:
        return False


def wait_for(
    parameters:    dict | None = None,
    response: Any = None,
    player:   Any = None,
    session_memory: Any = None,
) -> str:
    """Block until a condition is true or the timeout expires.

    Modes (pick ONE via `kind`):
      - kind="url_contains"  text="<substring>"           — browser URL contains
      - kind="url_changed"   from_url="<old>"             — URL differs from old
      - kind="page_contains" text="<substring>"           — page text contains
      - kind="file_exists"   path="<path>"                — file exists (and is non-empty when non_empty=true)
      - kind="file_removed"  path="<path>"                — file is gone
      - kind="http_ok"       url="<url>"                  — HEAD returns 2xx/3xx
      - kind="process"       name="<procname>" state=on   — a process matching the name is running (state=off → not running)
      - kind="seconds"       seconds=<n>                  — just sleep (upper bound still honoured)

    Returns a short status string the worker's verifier can key on:
      "wait_for OK — <mode> <detail>"        on success
      "wait_for TIMEOUT — <mode> <detail>"   on expiry
    """
    params = parameters or {}
    kind = str(params.get("kind", "")).lower().strip()
    timeout = _clip_timeout(params.get("timeout_s"))
    deadline = time.monotonic() + timeout

    if not kind:
        return "wait_for: specify kind (url_contains, url_changed, page_contains, file_exists, file_removed, http_ok, process, seconds)."

    if kind == "seconds":
        n = _clip_timeout(params.get("seconds", _DEFAULT_TIMEOUT))
        time.sleep(n)
        return f"wait_for OK — seconds slept {n:.1f}s"

    detail = ""
    while True:
        try:
            if kind == "url_contains":
                needle = str(params.get("text", ""))
                url = _browser_url() or ""
                detail = f"url={url[:120]}"
                if needle and needle.lower() in url.lower():
                    return f"wait_for OK — url_contains {needle!r}; {detail}"
            elif kind == "url_changed":
                old = str(params.get("from_url", ""))
                url = _browser_url() or ""
                detail = f"url={url[:120]}"
                if url and old and url != old:
                    return f"wait_for OK — url_changed to {url}"
            elif kind == "page_contains":
                needle = str(params.get("text", ""))
                txt = _browser_text()
                detail = f"page_len={len(txt)}"
                if needle and needle.lower() in txt.lower():
                    return f"wait_for OK — page_contains {needle!r}; {detail}"
            elif kind == "file_exists":
                p = Path(str(params.get("path", "")))
                non_empty = bool(params.get("non_empty", False))
                detail = f"path={p}"
                if p.exists() and (not non_empty or p.stat().st_size > 0):
                    return f"wait_for OK — file_exists {p}"
            elif kind == "file_removed":
                p = Path(str(params.get("path", "")))
                detail = f"path={p}"
                if not p.exists():
                    return f"wait_for OK — file_removed {p}"
            elif kind == "http_ok":
                url = str(params.get("url", ""))
                detail = f"url={url[:120]}"
                if url and _http_ok(url):
                    return f"wait_for OK — http_ok {url}"
            elif kind == "process":
                name = str(params.get("name", ""))
                want_on = str(params.get("state", "on")).lower() != "off"
                is_on = False
                if name:
                    try:
                        res = subprocess.run(
                            ["pgrep", "-if", name],
                            capture_output=True, text=True, timeout=3,
                        )
                        is_on = res.returncode == 0
                    except Exception:
                        is_on = False
                detail = f"name={name!r} on={is_on}"
                if is_on == want_on:
                    return f"wait_for OK — process {name!r} {'running' if is_on else 'gone'}"
            else:
                return f"wait_for: unknown kind {kind!r}"
        except Exception as e:
            return f"wait_for ERROR — {kind}: {type(e).__name__}: {e}"

        if time.monotonic() >= deadline:
            return f"wait_for TIMEOUT — {kind} after {timeout:.1f}s; {detail}"
        time.sleep(_POLL_SECONDS)


TOOL = {
    "name": "wait_for",
    "description": (
        "Wait until a condition is true (or a timeout expires). Use inside "
        "multi-step agent plans whenever the next step depends on something "
        "that takes a moment to settle — a page finishing to load, a file "
        "the user is about to drop onto the desktop, a download completing, "
        "a background process starting or stopping. Return value tells the "
        "verifier whether the wait succeeded (\"wait_for OK — …\") or ran "
        "out of time (\"wait_for TIMEOUT — …\"). Specify exactly one 'kind'. "
        "Never use this just to sleep in conversation — only inside an "
        "agent-planned step."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "kind": {
                "type": "STRING",
                "description": "url_contains | url_changed | page_contains | file_exists | file_removed | http_ok | process | seconds",
            },
            "text":     {"type": "STRING",  "description": "Substring to look for (url_contains, page_contains)."},
            "from_url": {"type": "STRING",  "description": "Previous URL (url_changed) — wait until the active URL differs."},
            "path":     {"type": "STRING",  "description": "Filesystem path (file_exists, file_removed)."},
            "non_empty":{"type": "BOOLEAN", "description": "For file_exists: also require size > 0."},
            "url":      {"type": "STRING",  "description": "HTTP URL to HEAD-check (http_ok)."},
            "name":     {"type": "STRING",  "description": "Process name pattern for pgrep (process)."},
            "state":    {"type": "STRING",  "description": "'on' or 'off' — whether the process should be running (process)."},
            "seconds":  {"type": "NUMBER",  "description": "Sleep duration in seconds (kind=seconds)."},
            "timeout_s":{"type": "NUMBER",  "description": "Hard timeout in seconds (default 30, max 600)."},
        },
        "required": ["kind"],
    },
    "handler": wait_for,
}
