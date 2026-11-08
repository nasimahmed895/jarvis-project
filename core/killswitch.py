"""
core/killswitch.py — emergency stop (P2.6).

A single authoritative "stop everything" switch. Three activation paths:

    1. Flag file at `~/.jarvis_killswitch` (filesystem — works across tools
       and from any shell / cron / remote SSH).
    2. Programmatic `trigger(reason)` — hotkey handlers and the dashboard
       button call this.
    3. Any thread polling `is_active()` and bailing early.

Consumers:
    - `core/worker.py:TaskWorker.run` — checks `is_active()` each tick; when
      True, cancels inflight work and sets its stop event.
    - TTS, Playwright browser contexts, any long-running action can wrap
      their main loop with `if killswitch.is_active(): return`.
    - UI banner / dashboard surfaces the active reason to the user.

Design decisions:
    - Latched. Once active, stays active until `reset()` is called from a
      trusted path (dashboard "resume" button or operator CLI). This
      prevents a race where the flag file is deleted mid-shutdown and
      half the subsystems resume while half do not.
    - Journaled. Every trigger and reset writes an event through
      `core.journal` so post-mortems have a definitive record.
    - Zero deps. Pure stdlib; loads before Qt, Playwright, or Gemini so
      the switch exists even when the rest of the process has not
      finished booting.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Callable

from core import journal as _journal
from core.logging_setup import get_logger

_log = get_logger(__name__)

# Flag file lives in the user home so it is reachable from any shell.
_FLAG_PATH = Path(os.environ.get("JARVIS_KILLSWITCH_FILE") or
                  (Path.home() / ".jarvis_killswitch"))

# Hot-path callers (worker tick, receive loop) ask `is_active()` many times
# per second. The authoritative check costs a `stat` syscall against the
# flag file; caching with a short TTL cuts it to one syscall per second
# without noticeably delaying the user's `touch` activation.
_FLAG_CHECK_TTL_S = 1.0

_state_lock = threading.RLock()
_active: bool = False
_reason: str | None = None
_triggered_at: float | None = None
_listeners: list[Callable[[str], None]] = []
_flag_cache_at: float = 0.0
_flag_cache_value: bool = False


# ── Public API ───────────────────────────────────────────────────────────────

def is_active() -> bool:
    """Fast check. Reads cached state first, then (on miss) stat's the flag
    file so an external process can arm the switch without IPC.

    The flag file stat is memoised for `_FLAG_CHECK_TTL_S` to keep this cheap
    inside hot loops. Latched state is checked without the TTL — once the
    switch is armed the answer is already definitive, no syscall needed."""
    global _flag_cache_at, _flag_cache_value
    with _state_lock:
        if _active:
            return True
    now = time.monotonic()
    if now - _flag_cache_at < _FLAG_CHECK_TTL_S:
        return _flag_cache_value
    present = False
    try:
        present = _FLAG_PATH.exists()
    except OSError:
        present = False
    _flag_cache_at = now
    _flag_cache_value = present
    if present:
        trigger("flag-file", _journal_event=False)
        return True
    return False


def trigger(reason: str, *, _journal_event: bool = True) -> None:
    """Latch the switch. Fires every registered listener, best-effort."""
    global _active, _reason, _triggered_at
    with _state_lock:
        if _active:
            return
        _active = True
        _reason = reason or "unspecified"
        _triggered_at = time.time()
        listeners = list(_listeners)
    _log.error("killswitch_triggered", extra={"reason": _reason})
    if _journal_event:
        _journal.write("killswitch_trigger", reason=_reason)
    for cb in listeners:
        try:
            cb(_reason or "unspecified")
        except Exception as e:
            _log.warning("killswitch_listener_err",
                         extra={"err": str(e)})


def reset() -> None:
    """Clear the switch. Removes the flag file if it exists. Trusted paths
    only (dashboard button, operator CLI)."""
    global _active, _reason, _triggered_at, _flag_cache_at, _flag_cache_value
    with _state_lock:
        was_active = _active
        _active = False
        prev_reason = _reason
        _reason = None
        _triggered_at = None
    try:
        if _FLAG_PATH.exists():
            _FLAG_PATH.unlink()
    except OSError:
        pass
    # Invalidate the stat cache so the next is_active() sees reality
    # rather than the pre-reset value.
    _flag_cache_at = 0.0
    _flag_cache_value = False
    if was_active:
        _log.warning("killswitch_reset", extra={"was_reason": prev_reason})
        _journal.write("killswitch_reset", was_reason=prev_reason)


def status() -> dict:
    with _state_lock:
        return {
            "active":       _active,
            "reason":       _reason,
            "triggered_at": _triggered_at,
            "flag_path":    str(_FLAG_PATH),
        }


def on_trigger(cb: Callable[[str], None]) -> None:
    """Register a listener fired once on activation."""
    with _state_lock:
        if cb not in _listeners:
            _listeners.append(cb)


def clear_listeners() -> None:
    """Testing helper."""
    with _state_lock:
        _listeners.clear()
