"""
Session handle persistence for Gemini Live.

The live session's resumption handle lets a reconnect continue the same
conversation rather than starting fresh. Before this module, that handle
lived only in RAM — the moment the process restarted, the conversation
was gone and the "yesterday we talked about…" line disappeared.

Trade-off: persisting across weeks is undesirable because the server
eventually drops the session on its side, and because the daily reflection
pass relies on a session boundary to summarise a day of talking. A short
TTL (12 h by default) is the balance: a boot in the morning after a
closed lid resumes last night's thread; a fresh clone or a week-later
restart does not.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional


DEFAULT_TTL_SECONDS = 12 * 3600.0


def _default_path() -> Path:
    return (Path(__file__).resolve().parent.parent
            / "memory" / "session_handle.json")


def save_handle(handle: str, *, path: Optional[Path] = None) -> bool:
    """Write handle + current timestamp. Non-fatal on error."""
    path = path or _default_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"handle": handle, "updated_at": time.time()}),
            encoding="utf-8",
        )
        return True
    except Exception as e:
        print(f"[SessionStore] save failed: {e}")
        return False


def load_handle(*,
                path: Optional[Path] = None,
                ttl_seconds: float = DEFAULT_TTL_SECONDS,
                now: float | None = None) -> Optional[str]:
    """Return the stored handle if it's younger than ``ttl_seconds``.
    Returns None otherwise (and quietly deletes the stale file so stale
    state doesn't linger forever)."""
    path = path or _default_path()
    now = now if now is not None else time.time()
    try:
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[SessionStore] load failed: {e}")
        return None
    handle = data.get("handle")
    updated = data.get("updated_at")
    if not handle or not isinstance(updated, (int, float)):
        _unlink(path)
        return None
    if (now - float(updated)) > ttl_seconds:
        _unlink(path)
        return None
    return str(handle)


def clear_handle(*, path: Optional[Path] = None) -> None:
    _unlink(path or _default_path())


def _unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass
