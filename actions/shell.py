"""
Bounded shell-exec tool.

Lets the assistant run short, non-interactive shell commands when the user
asks for one ("run pytest", "git status", "ls my downloads"). Everything is
bounded: a hard timeout, a sane working directory, stdout + stderr captured
and truncated, no stdin, no shell metacharacters when `args` is a list.

Confirmation: the first time a session runs `shell` the HUD gate asks for
permission (unless trust_me is on). Destructive patterns always re-ask — the
allowlist of force-refused commands catches the most dangerous ones even
with trust mode on.
"""
from __future__ import annotations

import os
import shlex
import signal
import subprocess
from pathlib import Path
from typing import Any


_DEFAULT_TIMEOUT = 20.0
_MAX_TIMEOUT     = 300.0
_OUT_CAP         = 8000   # characters per stream


# Hard refusals. We'd rather fail loudly than wipe a disk.
_REFUSE_PATTERNS = (
    "rm -rf /", "rm -rf ~", "rm -rf $HOME",
    "mkfs", ":(){:|:&};:", "dd if=", "> /dev/sda",
    "shutdown -h now", "halt -p", "poweroff",
    "curl | sh", "curl | bash", "wget | sh", "wget | bash",
)


def _refuse(cmd: str) -> str | None:
    low = cmd.lower().replace("  ", " ").strip()
    for p in _REFUSE_PATTERNS:
        if p in low:
            return f"shell refused: pattern {p!r} would damage the system."
    return None


def _trust() -> bool:
    try:
        from actions.browser_control import _trust_me
        return _trust_me()
    except Exception:
        return False


def shell(
    parameters:    dict | None = None,
    response: Any = None,
    player:   Any = None,
    session_memory: Any = None,
) -> str:
    """Run a shell command; return exit code + captured stdout/stderr."""
    params = parameters or {}
    cmd = params.get("command") or params.get("cmd") or ""
    if isinstance(cmd, list):
        cmd_str = " ".join(shlex.quote(str(x)) for x in cmd)
        argv: list[str] | None = [str(x) for x in cmd]
    else:
        cmd_str = str(cmd).strip()
        argv = None
    if not cmd_str:
        return "shell: provide 'command' (string or list)."
    refused = _refuse(cmd_str)
    if refused:
        return refused

    try:
        timeout = float(params.get("timeout_s") or _DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        timeout = _DEFAULT_TIMEOUT
    timeout = max(1.0, min(_MAX_TIMEOUT, timeout))

    cwd = params.get("cwd")
    if cwd:
        try:
            cwd_p = Path(os.path.expanduser(str(cwd))).resolve()
            if not cwd_p.exists() or not cwd_p.is_dir():
                return f"shell: cwd {cwd_p} does not exist."
        except Exception as e:
            return f"shell: bad cwd — {e}"
    else:
        cwd_p = Path.cwd()

    # Permission gate (unless trust_me).
    try:
        from core import confirm as _confirm
    except Exception:
        _confirm = None  # type: ignore
    if not _trust() and _confirm is not None and _confirm.is_bound():
        def _run_confirmed() -> str:
            return _run_cmd(argv, cmd_str, cwd_p, timeout)
        return _confirm.request(
            key=f"shell:{cmd_str[:40]}",
            title="Run a shell command",
            detail=(f"cwd: {cwd_p}\n{cmd_str[:200]}"),
            run=_run_confirmed,
        )
    return _run_cmd(argv, cmd_str, cwd_p, timeout)


def _run_cmd(argv: list[str] | None, cmd_str: str,
             cwd: Path, timeout: float) -> str:
    try:
        if argv is not None:
            proc = subprocess.run(
                argv, cwd=str(cwd), capture_output=True,
                text=True, timeout=timeout, start_new_session=True,
            )
        else:
            proc = subprocess.run(
                ["/bin/sh", "-c", cmd_str], cwd=str(cwd),
                capture_output=True, text=True,
                timeout=timeout, start_new_session=True,
            )
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "")[:_OUT_CAP]
        err = (e.stderr or "")[:_OUT_CAP]
        return (f"shell TIMEOUT after {timeout:.0f}s: {cmd_str[:200]}\n"
                f"--- stdout ---\n{out}\n--- stderr ---\n{err}")
    except FileNotFoundError as e:
        return f"shell: {e}"
    out = (proc.stdout or "")[:_OUT_CAP]
    err = (proc.stderr or "")[:_OUT_CAP]
    rc  = proc.returncode
    head = f"shell exit={rc} cwd={cwd} cmd={cmd_str[:200]}"
    return f"{head}\n--- stdout ---\n{out}\n--- stderr ---\n{err}"


TOOL = {
    "name": "shell",
    "description": (
        "Run one short, non-interactive shell command on the user's machine "
        "and return its exit code with captured stdout/stderr. Use for "
        "developer workflows the user asks for out loud — `git status`, "
        "`pytest -q`, `ls ~/Downloads`, `brew install X`, `ffmpeg -i …`. "
        "Not for websites (use browser_control), not for GUI apps (use "
        "open_app), not for anything that needs user input mid-run. "
        "Command runs under /bin/sh; timeout defaults to 20s (max 300). "
        "A few genuinely dangerous patterns (rm -rf /, mkfs, dd if=, "
        "shutdown) are refused outright even in trust mode."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "command": {
                "type": "STRING",
                "description": "Command line as a single string (runs under /bin/sh -c).",
            },
            "cwd": {
                "type": "STRING",
                "description": "Working directory. Default: current process cwd. Supports ~.",
            },
            "timeout_s": {
                "type": "NUMBER",
                "description": "Hard timeout in seconds (default 20, max 300).",
            },
        },
        "required": ["command"],
    },
    "handler": shell,
}
