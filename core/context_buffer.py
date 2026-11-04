"""
Multimodal context buffer.

Session-scoped ring buffer holding short summaries of every input
JARVIS has processed: voice turns, text commands, screenshots, camera
frames, OCR extractions, file reads, sensor readings. Lets the model
answer questions like "what did I just show you?" or "what was on the
screen a minute ago?" without each tool having to thread context
through its arguments.

Deliberately in-memory only (fed through a thread-safe deque). The
persistent memory store is for durable facts; this is the short tail
that a conversation uses *right now*. Nothing here claims to be a
long-lived perception — a reader can always confirm from the hashes
which modality produced each entry.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Iterable


_LOCK = threading.RLock()
_MAX_ITEMS = 200
_BUFFER: "Deque[dict]" = deque(maxlen=_MAX_ITEMS)


@dataclass
class Entry:
    kind:    str
    summary: str
    ts:      float = field(default_factory=time.time)
    meta:    dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"kind": self.kind, "summary": self.summary,
                "ts": self.ts, "meta": dict(self.meta)}


def record(kind: str, summary: str, *, meta: dict | None = None,
            ts: float | None = None) -> None:
    """Append one observation. Never raises — callers treat recording as
    best-effort so a missing buffer never breaks the main path."""
    try:
        entry = Entry(kind=str(kind or "unknown"),
                      summary=str(summary or "")[:600],
                      ts=ts if ts is not None else time.time(),
                      meta=dict(meta or {}))
        with _LOCK:
            _BUFFER.append(entry.to_dict())
    except Exception as e:  # pragma: no cover — defensive
        print(f"[ContextBuffer] record failed: {e}")


def recent(limit: int = 10,
            kinds: Iterable[str] | None = None,
            since_seconds: float | None = None) -> list[dict]:
    """Return the N most recent entries, newest first, optionally filtered
    by ``kinds`` (exact match or dotted prefix: 'vision' matches
    'vision:screen:ui') and ``since_seconds`` (age cap)."""
    with _LOCK:
        snap = list(_BUFFER)
    now = time.time()
    out = list(reversed(snap))
    if since_seconds is not None:
        out = [e for e in out if (now - float(e["ts"])) <= since_seconds]
    if kinds:
        tokens = [k.lower() for k in kinds]
        def _match(k: str) -> bool:
            k_low = k.lower()
            return any(k_low == t or k_low.startswith(t + ":") for t in tokens)
        out = [e for e in out if _match(e.get("kind", ""))]
    return out[:max(1, limit)]


def search(query: str, limit: int = 10) -> list[dict]:
    """Substring search across summaries (case-insensitive)."""
    q = (query or "").strip().lower()
    if not q:
        return []
    with _LOCK:
        snap = list(_BUFFER)
    return [e for e in reversed(snap)
            if q in (e.get("summary") or "").lower()][:max(1, limit)]


def clear() -> None:
    with _LOCK:
        _BUFFER.clear()


def size() -> int:
    with _LOCK:
        return len(_BUFFER)
