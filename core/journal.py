"""
Append-only event journal.

Every agent step dispatch and every tool run goes through here as one JSON
line. Phase 3 (reflection) reads it back to distil lessons; for now it is a
black box for post-mortems and a cheap audit trail. Files rotate daily under
`memory/journal/YYYY-MM-DD.jsonl` to keep each file easily grep-able.
"""
from __future__ import annotations

import datetime as _dt
import json
import threading
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()
_DIR: Path | None = None


def connect(base_dir: Path | str) -> None:
    global _DIR
    p = Path(base_dir)
    p.mkdir(parents=True, exist_ok=True)
    _DIR = p


def _today_path() -> Path:
    assert _DIR is not None, "journal.connect() not called"
    return _DIR / f"{_dt.date.today().isoformat()}.jsonl"


def write(event: str, **fields: Any) -> None:
    """Record one event. `event` is a short label; `fields` carries data.
    Never raises — a journal error must not kill the agent loop."""
    if _DIR is None:
        return
    try:
        rec = {"ts": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
               "event": event, **fields}
        with _LOCK:
            with _today_path().open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    except Exception as e:
        print(f"[Journal] write failed: {e}")


def read_recent(limit: int = 200, event: str | None = None) -> list[dict]:
    """Read the most recent entries from today's file. Cheap — intended for
    status probes and the reflection pass in Phase 3."""
    if _DIR is None:
        return []
    p = _today_path()
    if not p.exists():
        return []
    try:
        with _LOCK:
            lines = p.read_text(encoding="utf-8").splitlines()[-limit:]
    except Exception:
        return []
    out: list[dict] = []
    for line in lines:
        try:
            r = json.loads(line)
            if event and r.get("event") != event:
                continue
            out.append(r)
        except Exception:
            continue
    return out
