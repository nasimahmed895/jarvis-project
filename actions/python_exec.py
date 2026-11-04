"""
Sandboxed Python exec tool.

Lets the assistant run short Python snippets when the user asks for a quick
computation, data wrangle, regex check, or JSON munge. Runs in a *subprocess*
so a bad snippet cannot wreck JARVIS: we do not share the live process
memory, and we kill the subprocess at the deadline.

Not a REPL — each call is a fresh process. For persistent state, write to a
file. Same hard-refusal patterns and trust gate as `shell`.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


_DEFAULT_TIMEOUT = 15.0
_MAX_TIMEOUT     = 120.0
_OUT_CAP         = 8000


_REFUSE_SUBSTRINGS = (
    "os.system('rm -rf /')",
    "shutil.rmtree('/')",
    "while True: pass",
)


def _refuse(code: str) -> str | None:
    low = code.replace(" ", "").lower()
    for p in _REFUSE_SUBSTRINGS:
        if p.replace(" ", "").lower() in low:
            return f"python_exec refused: suspicious pattern {p!r}."
    return None


def _trust() -> bool:
    try:
        from actions.browser_control import _trust_me
        return _trust_me()
    except Exception:
        return False


def python_exec(
    parameters:    dict | None = None,
    response: Any = None,
    player:   Any = None,
    session_memory: Any = None,
) -> str:
    """Execute a Python snippet in a fresh subprocess."""
    params = parameters or {}
    code = str(params.get("code", "")).strip()
    if not code:
        return "python_exec: provide 'code'."
    refused = _refuse(code)
    if refused:
        return refused

    try:
        timeout = float(params.get("timeout_s") or _DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        timeout = _DEFAULT_TIMEOUT
    timeout = max(1.0, min(_MAX_TIMEOUT, timeout))

    # Permission gate (unless trust_me).
    try:
        from core import confirm as _confirm
    except Exception:
        _confirm = None  # type: ignore
    if not _trust() and _confirm is not None and _confirm.is_bound():
        def _run_confirmed() -> str:
            return _run(code, timeout)
        preview = code if len(code) < 240 else code[:240] + "…"
        return _confirm.request(
            key=f"python:{hash(code) & 0xffff}",
            title="Run a Python snippet",
            detail=preview,
            run=_run_confirmed,
        )
    return _run(code, timeout)


def _run(code: str, timeout: float) -> str:
    with tempfile.NamedTemporaryFile(
        "w", suffix=".py", delete=False, encoding="utf-8",
    ) as f:
        f.write(code)
        path = f.name
    try:
        proc = subprocess.run(
            [sys.executable, "-I", "-S", path],
            capture_output=True, text=True, timeout=timeout,
            start_new_session=True,
        )
    except subprocess.TimeoutExpired as e:
        return (f"python_exec TIMEOUT after {timeout:.0f}s\n"
                f"--- stdout ---\n{(e.stdout or '')[:_OUT_CAP]}\n"
                f"--- stderr ---\n{(e.stderr or '')[:_OUT_CAP]}")
    finally:
        try:
            os.unlink(path)
        except Exception:
            pass
    out = (proc.stdout or "")[:_OUT_CAP]
    err = (proc.stderr or "")[:_OUT_CAP]
    rc  = proc.returncode
    return (f"python_exec exit={rc}\n--- stdout ---\n{out}\n"
            f"--- stderr ---\n{err}")


TOOL = {
    "name": "python_exec",
    "description": (
        "Run a short Python snippet in a fresh subprocess and return its "
        "stdout/stderr + exit code. Use for quick computations, JSON/CSV "
        "wrangling, regex checks, string munging, reading or writing a "
        "known file, calling a library you installed. Not a REPL — state "
        "does not persist between calls; write to a file if you need "
        "continuity. Timeout defaults to 15s (max 120). Pre-blocks a few "
        "classic footguns (`rm -rf /`, `while True: pass`, etc.) even in "
        "trust mode."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "code": {
                "type": "STRING",
                "description": "Python 3 source. stdout is captured and returned.",
            },
            "timeout_s": {
                "type": "NUMBER",
                "description": "Hard timeout in seconds (default 15, max 120).",
            },
        },
        "required": ["code"],
    },
    "handler": python_exec,
}
