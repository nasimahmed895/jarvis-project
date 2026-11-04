"""
Daily reflection pass.

Reads yesterday's journal, asks a SMART model to distil 3-10 short lessons
for JARVIS — mistakes to avoid, patterns worth repeating, user preferences
that surfaced as corrections. Writes them into long_term memory under a
new category `learned`, each keyed by date so growth is visible.

Runs at most once per 20 hours. Quietly no-ops when the journal is empty or
the model declines to summarise.
"""
from __future__ import annotations

import datetime as _dt
import json
import time
from pathlib import Path
from typing import Any

from core import gemini as _gem
from core import journal as _journal


_MIN_HOURS_BETWEEN = 20.0
_MAX_EVENTS = 300  # cap how much of the journal rides into the prompt
_STATE_FILE: Path | None = None


def connect(state_file: Path | str) -> None:
    global _STATE_FILE
    p = Path(state_file)
    p.parent.mkdir(parents=True, exist_ok=True)
    _STATE_FILE = p


def _load_state() -> dict:
    if _STATE_FILE is None or not _STATE_FILE.exists():
        return {}
    try:
        return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    if _STATE_FILE is None:
        return
    try:
        _STATE_FILE.write_text(
            json.dumps(state, indent=2, default=str), encoding="utf-8"
        )
    except Exception as e:
        print(f"[Reflection] state write failed: {e}")


def _due() -> bool:
    last = _load_state().get("last_reflection_ts", 0.0)
    try:
        last = float(last)
    except (TypeError, ValueError):
        last = 0.0
    hours = (time.time() - last) / 3600.0
    return hours >= _MIN_HOURS_BETWEEN


def _mark_done() -> None:
    state = _load_state()
    state["last_reflection_ts"] = time.time()
    _save_state(state)


_SYSTEM = """\
You are the reflection pass of a voice assistant. You will be given the raw
event log from the last 24 hours — tool starts, successes, verification
failures, repair attempts, user corrections. Distil 3 to 10 short lessons
for the assistant to remember.

Each lesson is one sentence. Favour lessons that:
  - name a pattern that wasted time (a tool we called that always fails on
    this user's machine, a verifier that is too strict, a repair the model
    kept guessing wrong).
  - record a user correction ("user prefers X over Y").
  - capture a sequence that worked well so the assistant repeats it.

Skip anything that is obvious, one-off, or private (API keys, passwords,
personal data unless clearly a preference). If there is genuinely nothing
worth distilling, return an empty lessons array.

Return ONLY a JSON object:
  {"lessons": ["<sentence>", "<sentence>", ...]}
No markdown, no commentary.
"""


def run_if_due(log=print) -> int:
    """If enough time has passed since the last reflection, run one and
    write results to long-term memory. Returns the number of lessons saved.
    Never raises — a failure just logs."""
    try:
        if not _due():
            return 0
        events = _journal.read_recent(limit=_MAX_EVENTS)
        if not events:
            return 0
        _mark_done()
        # Trim each event line to keep the prompt bounded.
        payload = "\n".join(
            json.dumps({k: v for k, v in e.items()
                        if k in ("ts", "event", "tool", "verb",
                                 "error", "why", "args",
                                 "result", "task_id", "step_id")},
                       default=str)[:400]
            for e in events
        )
        resp = _gem.as_json(
            [_SYSTEM, payload], tier=_gem.SMART,
            timeout_ms=30_000, default=None,
        )
        if not isinstance(resp, dict):
            log("[Reflection] no response")
            return 0
        lessons = resp.get("lessons") or []
        if not isinstance(lessons, list) or not lessons:
            log("[Reflection] no lessons extracted")
            return 0
        _write_lessons(lessons, log=log)
        log(f"[Reflection] distilled {len(lessons)} lesson(s)")
        return len(lessons)
    except Exception as e:
        log(f"[Reflection] error: {e}")
        return 0


def _write_lessons(lessons: list[str], log=print) -> None:
    """Append lessons to long_term memory under a `learned` category with
    one entry per day. Avoids duplicates: if the day's key already exists,
    extends its value with a bullet-prefixed newline for each new lesson.

    Each genuinely new lesson is also indexed into the semantic-vector store
    under source='lessons' so semantic_recall can surface it when the model
    asks a related question. Indexing is best-effort — a vectors module that
    hasn't been connect()-ed yet just no-ops."""
    from memory.memory_manager import load_memory, update_memory
    today = _dt.date.today().isoformat()
    mem = load_memory()
    existing = (mem.get("learned") or {}).get(today, {}).get("value", "")
    merged = existing
    new_lines: list[str] = []
    for ls in lessons:
        text = str(ls).strip()
        if not text:
            continue
        line = f"• {text}"
        if line not in merged:
            merged = (merged + "\n" + line).strip() if merged else line
            new_lines.append(text)
    update_memory({"learned": {today: {"value": merged}}})

    for text in new_lines:
        try:
            from core import vectors as _vectors
            _vectors.add("lessons", text, source_id=f"{today}:{text[:40]}")
        except Exception as e:
            log(f"[Reflection] lesson index skipped: {type(e).__name__}: {e}")
