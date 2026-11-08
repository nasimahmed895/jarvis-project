"""
actions/file_watcher.py — file change monitor (P2.5).

Stdlib polling implementation. `watchdog` would be a cleaner backend (inotify
on Linux, FSEvents on macOS) but adding a dep for a feature that already
ships value through polling is not worth it. The polling interval defaults
to 5s — plenty for the "drop a file into a folder and Jarvis notices"
workflow this unblocks.

Events emitted: created / modified / deleted. Each event is a dict:

    {"event": "created|modified|deleted",
     "path":  "<absolute path>",
     "size":  <bytes or 0>,
     "mtime": <unix seconds>,
     "watcher": "<name>"}

By default events are appended to the structured journal (`core.journal`).
A custom `event_cb` lets callers plug the monitor into the notify queue,
the worker, or a dashboard panel.
"""
from __future__ import annotations

import fnmatch
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable

from core import journal as _journal
from core.logging_setup import get_logger

_log = get_logger(__name__)


class FileWatcher:
    """One watcher = one target path. Thread-safe — `start`/`stop`/`poll` can
    be called from any thread; the scan body is serialized by an internal
    lock."""

    def __init__(self, path: str | Path, name: str = "",
                 patterns: Iterable[str] = ("*",),
                 recursive: bool = True,
                 interval_s: float = 5.0,
                 event_cb: Callable[[dict], None] | None = None):
        self.name = name or Path(path).name or "watcher"
        self.path = Path(path).expanduser().resolve()
        self.patterns = tuple(patterns)
        self.recursive = recursive
        self.interval_s = interval_s
        self._event_cb = event_cb or _default_sink
        self._snapshot: dict[str, tuple[float, int]] = {}
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop_evt = threading.Event()

    # ── Life cycle ───────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        # Seed the snapshot so the first poll only reports deltas, not every
        # existing file as "created".
        self._refresh(emit=False)
        self._stop_evt.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"watcher:{self.name}", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_evt.set()
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None

    # ── Polling ──────────────────────────────────────────────────────────

    def poll(self) -> list[dict]:
        """Run one scan and return events emitted (also sent to callback)."""
        return self._refresh(emit=True)

    def _refresh(self, emit: bool) -> list[dict]:
        seen: dict[str, tuple[float, int]] = {}
        events: list[dict] = []
        for p in self._walk():
            try:
                st = p.stat()
            except OSError:
                continue
            key = str(p)
            seen[key] = (st.st_mtime, st.st_size)
        with self._lock:
            # Compare against the previous snapshot.
            for key, (mtime, size) in seen.items():
                prev = self._snapshot.get(key)
                if prev is None:
                    event = "created"
                elif prev[0] != mtime or prev[1] != size:
                    event = "modified"
                else:
                    continue
                events.append(self._event(event, key, size, mtime))
            for key, (mtime, size) in self._snapshot.items():
                if key not in seen:
                    events.append(self._event("deleted", key, size, mtime))
            self._snapshot = seen
        if emit:
            for e in events:
                try:
                    self._event_cb(e)
                except Exception as exc:
                    _log.warning("watcher_cb_err", extra={"err": str(exc)})
        return events

    def _event(self, event: str, path: str, size: int, mtime: float) -> dict:
        return {
            "event":   event,
            "path":    path,
            "size":    size,
            "mtime":   mtime,
            "watcher": self.name,
        }

    def _walk(self):
        if not self.path.exists():
            return
        if self.path.is_file():
            if self._matches(self.path.name):
                yield self.path
            return
        if not self.recursive:
            for entry in self.path.iterdir():
                if entry.is_file() and self._matches(entry.name):
                    yield entry
            return
        for dirpath, _dirnames, filenames in os.walk(self.path):
            for name in filenames:
                if self._matches(name):
                    yield Path(dirpath) / name

    def _matches(self, filename: str) -> bool:
        return any(fnmatch.fnmatch(filename, pat) for pat in self.patterns)

    def _run(self):
        while not self._stop_evt.is_set():
            try:
                self._refresh(emit=True)
            except Exception as e:
                _log.error("watcher_loop_err", extra={"err": str(e)})
            self._stop_evt.wait(self.interval_s)


# ── Default sink + registry ───────────────────────────────────────────────

def _default_sink(event: dict) -> None:
    _journal.write(
        "file_change",
        event=event["event"], path=event["path"],
        size=event["size"], watcher=event["watcher"],
    )


_REGISTRY: dict[str, FileWatcher] = {}
_REGISTRY_LOCK = threading.Lock()


def register(name: str, path: str, patterns: list[str] | None = None,
             recursive: bool = True, interval_s: float = 5.0) -> str:
    with _REGISTRY_LOCK:
        if name in _REGISTRY:
            return f"Watcher '{name}' already registered."
        w = FileWatcher(
            path=path, name=name,
            patterns=patterns or ["*"], recursive=recursive,
            interval_s=interval_s,
        )
        w.start()
        _REGISTRY[name] = w
        return f"Watching '{path}' as '{name}'."


def unregister(name: str) -> str:
    with _REGISTRY_LOCK:
        w = _REGISTRY.pop(name, None)
    if not w:
        return f"No watcher '{name}'."
    w.stop()
    return f"Stopped watcher '{name}'."


def list_watchers() -> list[dict]:
    with _REGISTRY_LOCK:
        return [{
            "name":      w.name,
            "path":      str(w.path),
            "patterns":  list(w.patterns),
            "recursive": w.recursive,
            "interval_s": w.interval_s,
        } for w in _REGISTRY.values()]


# ── Tool dispatch ─────────────────────────────────────────────────────────

def _tool_entry(parameters: dict, **_kw: Any) -> str:
    action = (parameters.get("action") or "list").lower()
    name = parameters.get("name") or "default"
    if action == "add":
        path = parameters.get("path")
        if not path:
            return "`path` is required for add."
        patterns = parameters.get("patterns") or ["*"]
        if isinstance(patterns, str):
            patterns = [patterns]
        return register(
            name=name, path=path, patterns=list(patterns),
            recursive=bool(parameters.get("recursive", True)),
            interval_s=float(parameters.get("interval_s", 5.0)),
        )
    if action == "remove":
        return unregister(name)
    if action == "list":
        rows = list_watchers()
        if not rows:
            return "No active watchers."
        return "\n".join(
            f"  · {r['name']} — {r['path']} ({','.join(r['patterns'])})"
            for r in rows
        )
    return f"Unknown action '{action}'. Try add | remove | list."


TOOL = {
    "name": "file_watcher",
    "description": (
        "Watch a path for new / modified / deleted files. Each change is "
        "recorded to the activity journal. Use when the user wants Jarvis "
        "to notice when a file appears (download finished, screenshot "
        "captured, document saved)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action":     {"type": "string", "enum": ["add", "remove", "list"]},
            "name":       {"type": "string"},
            "path":       {"type": "string"},
            "patterns":   {"type": "array", "items": {"type": "string"}},
            "recursive":  {"type": "boolean"},
            "interval_s": {"type": "number"},
        },
        "required": ["action"],
    },
    "run": _tool_entry,
}
