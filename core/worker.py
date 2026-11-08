"""
Agent worker loop.

Lives as a long-running asyncio task inside the main Live session TaskGroup.
Polls the task store for ready steps, dispatches each to the registered
action handler (or the inline tool dispatcher passed in), verifies the
result, and advances or fails the step. Reports progress through a notify
callback — in production that is `JarvisLive.plugin_say` + `ui.write_log`,
so the user hears the agent finishing a task without having to ask.

Resilience:
  - A tool raising just fails that step; other tasks keep moving.
  - On startup, any step left in 'running' from a prior crash is reset
    back to 'planned' — the worker tries again rather than leaving zombies.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import traceback
from typing import Any, Awaitable, Callable

from core import tasks as _tasks
from core import journal as _journal
from core import gemini as _gem
from core import planner as _planner


# How often to poll for ready work. The main loop is event-driven for user
# utterances; the worker is a background heartbeat, so a modest tick is fine.
_POLL_SECONDS = 0.5

# Max concurrent step dispatches per tick. The tool handlers are mostly
# executor-bound (sync), so running many at once mainly costs threads; three
# is plenty for a single human user.
_MAX_INFLIGHT = 3

# Each step may be repaired at most this many times before it is left as
# failed. One repair covers "tool arg was slightly wrong"; two covers a
# chain of retries; three stops an infinite loop.
_MAX_REPAIRS = 2

# Upper bound on strategic replans per task. Replanning is coarser than
# arg-level repair: on terminal failure, the SMART model is asked to amend
# the remaining plan. Two attempts is enough to escape a dead end without
# risking an infinite loop of replans that all fail the same way.
_MAX_REPLANS = 2


Dispatcher = Callable[[str, dict], Awaitable[str]]
Notifier = Callable[[str], None]
DeclarationProvider = Callable[[], list[dict]]


class TaskWorker:
    """Owns one asyncio task that drains ready steps from the SQLite store.

    Parameters
    ----------
    dispatch : Dispatcher
        async callable (tool_name, args) → result string. Should raise on
        failure; the worker catches and records the exception. Caller wires
        this to whatever can actually run a tool — in JARVIS that is a thin
        shim that routes through TOOL_DECLARATIONS, action_registry and
        plugin_registry.
    notify : Notifier
        Called with human-readable progress lines. Non-blocking, best-effort.
    """

    def __init__(self, dispatch: Dispatcher,
                 notify: Notifier | None = None,
                 declarations: DeclarationProvider | None = None):
        self._dispatch = dispatch
        self._notify = notify or (lambda _m: None)
        # Declaration provider is read lazily on each replan so plugins that
        # hot-register tools are reflected without a worker restart. Absent
        # provider disables replanning (worker still runs the arg-level repair
        # path exactly as before).
        self._declarations = declarations
        self._stop = asyncio.Event()
        self._inflight: dict[int, asyncio.Task] = {}

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        """Main loop. Awaits until stop() is called or cancelled."""
        self._rehydrate()
        self._notify("SYS: Agent worker online.")
        try:
            while not self._stop.is_set():
                try:
                    await self._tick()
                except Exception as e:
                    print(f"[Worker] tick error: {e}")
                    traceback.print_exc()
                await asyncio.sleep(_POLL_SECONDS)
        finally:
            for t in list(self._inflight.values()):
                t.cancel()

    def _rehydrate(self) -> None:
        """Any step left 'running' from a prior crash is reset to 'planned'
        so the next tick will retry it. Tasks whose status is 'running'
        stay that way — we have no way to know if they finished, so let the
        step state dictate truth."""
        for s in _tasks.list_tasks(status="running"):
            for step in _tasks.task_steps(s["id"]):
                if step["status"] == "running":
                    _tasks.set_step_status(
                        step["id"], "planned",
                        error="(restarted after crash)",
                    )

    async def _tick(self) -> None:
        # Reap finished dispatches.
        for sid in list(self._inflight):
            t = self._inflight[sid]
            if t.done():
                self._inflight.pop(sid, None)
                # Any uncaught exception is already stored by _run_step; log
                # anything that slipped past.
                try:
                    t.result()
                except asyncio.CancelledError:
                    pass
                except Exception as e:  # defensive
                    print(f"[Worker] step {sid} dispatch lost: {e}")

        free = _MAX_INFLIGHT - len(self._inflight)
        if free <= 0:
            return
        ready = _tasks.ready_steps(limit=free)
        for step in ready:
            if step["id"] in self._inflight:
                continue
            _tasks.set_step_status(step["id"], "running")
            # First running step promotes its parent task.
            parent = _tasks.get_task(step["task_id"])
            if parent and parent["status"] == "planned":
                _tasks.set_task_status(step["task_id"], "running")
                self._notify(f"SYS: Task #{step['task_id']} started — {parent['goal'][:60]}")
            self._inflight[step["id"]] = asyncio.create_task(self._run_step(step))

    async def _run_step(self, step: dict) -> None:
        sid = step["id"]
        tool = step["tool"]
        args = dict(step["args"] or {})
        verb = step["verb"]
        verify = step.get("verify") or {}
        task_id = step["task_id"]
        repairs = 0
        _journal.write("step_start", task_id=task_id, step_id=sid,
                       tool=tool, verb=verb, args=args)
        try:
            while True:
                pre_url = (_browser_url()
                           if verify.get("kind") == "url_changed" else None)
                if tool == "speak":
                    text = str(args.get("text", ""))
                    self._notify(f"SYS: {verb}: {text}")
                    _tasks.set_step_status(sid, "done", result=text)
                    _journal.write("step_done", step_id=sid, verb=verb,
                                   tool=tool, result=text, repairs=repairs)
                    return
                timeout_s = step.get("timeout_s")
                try:
                    if timeout_s and float(timeout_s) > 0:
                        result = await asyncio.wait_for(
                            self._dispatch(tool, args),
                            timeout=float(timeout_s),
                        )
                    else:
                        result = await self._dispatch(tool, args)
                except asyncio.TimeoutError:
                    err = f"timeout after {timeout_s}s"
                    _journal.write("step_timeout", step_id=sid, tool=tool,
                                   args=args, timeout_s=timeout_s,
                                   repairs=repairs)
                    _tasks.set_step_status(sid, "failed", error=err)
                    if await self._attempt_replan(task_id, step, err):
                        return
                    self._notify(f"SYS: ✗ {verb} — {err}")
                    return
                except Exception as e:
                    err = f"{type(e).__name__}: {str(e)[:200]}"
                    _journal.write("step_error", step_id=sid, tool=tool,
                                   args=args, error=err, repairs=repairs)
                    if repairs < _MAX_REPAIRS:
                        patched = await self._repair(step, args, err)
                        if patched is not None:
                            args = patched
                            repairs += 1
                            continue
                    _tasks.set_step_status(sid, "failed", error=err)
                    if await self._attempt_replan(task_id, step, err):
                        return
                    self._notify(f"SYS: ✗ {verb} — {err}")
                    return
                ok, why = _verify(verify or None, result, tool, args,
                                  pre_url=pre_url)
                if ok:
                    _tasks.set_step_status(sid, "done", result=str(result)[:4000])
                    self._notify(f"SYS: ✓ {verb}")
                    _journal.write("step_done", step_id=sid, verb=verb,
                                   tool=tool,
                                   result=str(result)[:600], repairs=repairs)
                    return
                _journal.write("step_verify_fail", step_id=sid, tool=tool,
                               verify=verify, result=str(result)[:400],
                               why=why, repairs=repairs)
                if repairs < _MAX_REPAIRS:
                    patched = await self._repair(step, args, why,
                                                  last_result=str(result)[:400])
                    if patched is not None:
                        args = patched
                        repairs += 1
                        continue
                _tasks.set_step_status(sid, "failed",
                                       result=str(result)[:4000],
                                       error=why)
                if await self._attempt_replan(task_id, step, why):
                    return
                self._notify(f"SYS: ✗ {verb} — {why}")
                return
        except asyncio.CancelledError:
            _tasks.set_step_status(sid, "cancelled")
            _journal.write("step_cancel", step_id=sid)
            raise
        finally:
            self._maybe_finalise_task(task_id)

    async def _repair(self, step: dict, last_args: dict, error: str,
                      last_result: str | None = None) -> dict | None:
        """Ask the SMART model for a single retry with adjusted args.
        Returns a replacement `args` dict, or None when repair is not
        appropriate. Never calls another tool itself — just rewrites args."""
        prompt = (
            "You are the repair brain of a voice-assistant agent. A tool call "
            "failed. Decide if a SINGLE retry with adjusted arguments might "
            "work. Respond with JSON: either {\"retry\": true, \"args\": {...}} "
            "with the full replacement args object, or {\"retry\": false, "
            "\"reason\": \"<short>\"}. Never invent a different tool.\n\n"
            f"Step: {step.get('verb')}\n"
            f"Tool: {step.get('tool')}\n"
            f"Previous args: {json.dumps(last_args)}\n"
            f"Error / verifier message: {error}\n"
            f"Last tool result (if any): {last_result or '(none)'}\n"
        )
        loop = asyncio.get_event_loop()
        try:
            resp = await loop.run_in_executor(
                None,
                lambda: _gem.as_json([prompt], tier=_gem.FAST,
                                     timeout_ms=15_000, default=None),
            )
        except Exception as e:
            print(f"[Worker] repair llm err: {e}")
            return None
        if not isinstance(resp, dict) or not resp.get("retry"):
            return None
        new_args = resp.get("args")
        if not isinstance(new_args, dict):
            return None
        _journal.write("step_repair", step_id=step["id"],
                       tool=step["tool"], old=last_args, new=new_args)
        self._notify(f"SYS: ↻ retrying {step.get('verb')} with repair")
        return new_args

    async def _attempt_replan(self, task_id: int, failed_step: dict,
                              reason: str) -> bool:
        """When arg-level repair is exhausted, ask the planner to amend the
        remaining plan. Returns True if a new plan was committed (the task
        stays alive and the worker will pick up the new steps); False if
        replanning is disabled, over its budget, or produced no steps."""
        if self._declarations is None:
            return False
        try:
            used = _tasks.get_replan_count(task_id)
        except Exception as e:
            print(f"[Worker] replan count read failed: {e}")
            return False
        if used >= _MAX_REPLANS:
            return False
        task = _tasks.get_task(task_id)
        if not task:
            return False
        done_steps = [
            s for s in _tasks.task_steps(task_id)
            if s.get("status") == "done"
        ]
        try:
            declarations = list(self._declarations() or [])
        except Exception as e:
            print(f"[Worker] declaration provider failed: {e}")
            return False
        loop = asyncio.get_event_loop()
        try:
            new_steps = await loop.run_in_executor(
                None,
                lambda: _planner.replan_from(
                    task_id=task_id,
                    goal=task["goal"],
                    done_steps=done_steps,
                    failed_step=failed_step,
                    failure_reason=reason,
                    declarations=declarations,
                    log=lambda m: print(m),
                ),
            )
        except _planner.PlanError as e:
            _journal.write("replan_fail", task_id=task_id,
                           step_id=failed_step["id"], error=str(e))
            return False
        except Exception as e:
            _journal.write("replan_fail", task_id=task_id,
                           step_id=failed_step["id"],
                           error=f"{type(e).__name__}: {e}")
            return False
        if not new_steps:
            return False
        try:
            row_ids = _planner.commit_replan(task_id, new_steps)
        except Exception as e:
            print(f"[Worker] replan commit failed: {e}")
            return False
        _tasks.bump_replan_count(task_id)
        _journal.write("replan_ok", task_id=task_id,
                       step_id=failed_step["id"], reason=reason[:300],
                       new_step_ids=row_ids)
        self._notify(
            f"SYS: ↻ Replanned task #{task_id} after '{failed_step.get('verb','?')}' "
            f"— {len(new_steps)} new step(s)."
        )
        return True

    def _maybe_finalise_task(self, task_id: int) -> None:
        steps = _tasks.task_steps(task_id)
        if not steps:
            return
        statuses = {s["status"] for s in steps}
        if statuses & {"planned", "ready", "running", "awaiting_user"}:
            return
        # Everything is terminal. Decide the task's final state.
        if "failed" in statuses:
            _tasks.set_task_status(task_id, "failed")
            t = _tasks.get_task(task_id)
            if t:
                self._notify(f"SYS: Task #{task_id} failed — {t['goal'][:60]}")
        elif "cancelled" in statuses and "done" not in statuses:
            _tasks.set_task_status(task_id, "cancelled")
        else:
            _tasks.set_task_status(task_id, "done")
            t = _tasks.get_task(task_id)
            if t:
                self._notify(f"SYS: ✓ Task #{task_id} done — {t['goal'][:60]}")


def _verify(spec: dict | None, result: Any, tool: str, args: dict,
            pre_url: str | None = None) -> tuple[bool, str]:
    """Check a step's declared verifier against its result. Returns (ok, why).

    `pre_url` is the browser's active URL captured BEFORE this step ran, for
    the `url_changed` verifier. Modes:
      result_contains   — tool returned string contains text
      result_not_empty  — tool returned a non-blank string
      url_contains      — current browser URL contains text
      url_changed       — browser URL differs from pre_url
      page_contains     — current page body text contains text
      file_exists       — a filesystem path exists (and non_empty when set)
    """
    if not spec:
        return True, ""
    kind = str(spec.get("kind", "")).lower()
    text = str(result or "")
    if kind == "result_contains":
        needle = str(spec.get("text", ""))
        return (needle in text, f"missing substring {needle!r}")
    if kind == "result_not_empty":
        return (bool(text.strip()), "empty result")
    if kind == "exit_zero":
        # For shell / python_exec: the handler reports "exit=N" at the head.
        import re as _re
        m = _re.search(r"exit=(-?\d+)", text)
        if not m:
            return (False, "no exit code reported")
        return (m.group(1) == "0", f"exit={m.group(1)}")
    if kind == "stdout_contains":
        # Narrow version of result_contains that only looks at the stdout
        # region of shell / python_exec output.
        needle = str(spec.get("text", ""))
        parts = text.split("--- stdout ---", 1)
        if len(parts) < 2:
            return (needle in text, f"missing substring {needle!r}")
        stdout = parts[1].split("--- stderr ---", 1)[0]
        return (needle in stdout, f"stdout did not contain {needle!r}")
    if kind == "url_contains":
        needle = str(spec.get("text", ""))
        now = _browser_url() or ""
        return (needle.lower() in now.lower(),
                f"url is {now[:120]!r}; wanted substring {needle!r}")
    if kind == "url_changed":
        now = _browser_url() or ""
        if pre_url is None:
            return (bool(now), "no URL observable")
        return (now and now != pre_url,
                f"url still {now[:120]!r}")
    if kind == "page_contains":
        needle = str(spec.get("text", ""))
        page = _browser_text()
        return (needle.lower() in page.lower(),
                f"page {len(page)} chars did not contain {needle!r}")
    if kind == "file_exists":
        from pathlib import Path as _P
        p = _P(str(spec.get("path", "")))
        non_empty = bool(spec.get("non_empty", False))
        ok = p.exists() and (not non_empty or p.stat().st_size > 0)
        return (ok, f"missing path {p}")
    # Unknown verifier → don't block on it.
    return True, ""


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
