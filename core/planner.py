"""
Goal → steps planner.

Calls a Gemini SMART model with the live tool catalogue and asks for a short
JSON plan. The plan is validated against the catalogue (no made-up tools, no
dependency cycles) before being committed to the task store.

Every step is one tool call. The worker picks them up in dependency order,
runs them, verifies, and marks done. The planner itself never runs tools.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Iterable

from core import gemini as _gem
from core import tasks as _tasks


# How many steps a single plan may contain. Guard against runaway plans; a
# goal that needs more is better split by the user into separate asks.
_MAX_STEPS = 24


_SYSTEM_PROMPT = """\
You are the planning brain of a voice assistant.

Your only job: take the user's goal and produce a JSON plan that is a list of
1 to {max_steps} concrete, single-tool steps. The worker will execute each
step with the named tool and arguments. You do NOT execute anything yourself.

Rules:
  1. Every step must use a tool that appears in the TOOL CATALOGUE below. Do
     not invent tools. Do not merge several actions into one step.
  2. Each step is an object with keys: id (1-based integer), verb (3-6 word
     human label), tool (tool name, exact), args (object, may be empty),
     deps (list of step ids this depends on, empty for independent steps),
     verify (optional short spec — see VERIFIER SPEC).
  3. Order the array in dependency order. A step may only depend on steps
     with smaller id.
  4. Prefer parallelism where it is safe — leave deps empty when steps do
     not read each other's output.
  5. For browser work, start with action=\"get_state\" if the current browser
     state matters. Never call open_app for websites — use browser_control
     go_to instead.
  6. Keep it short. Do not plan verifications of verifications. If a step's
     outcome is self-evident (e.g. closing a tab), omit verify.
  7. If the goal is a single request that fits one tool call, return a
     one-step plan — that is still a valid plan.
  8. If the goal is genuinely impossible with the given tools, return a plan
     with a single step using tool \"speak\" and args {{\"text\": \"<honest
     explanation>\"}}. (The worker treats speak as a no-op that just logs.)

VERIFIER SPEC (optional per step):
  {{\"kind\": \"result_contains\", \"text\": \"<substring\">}}
  {{\"kind\": \"result_not_empty\"}}
  {{\"kind\": \"stdout_contains\", \"text\": \"<substring>\"}}   (shell/python_exec — stdout only)
  {{\"kind\": \"exit_zero\"}}                                     (shell/python_exec)
  {{\"kind\": \"url_contains\", \"text\": \"<substring>\"}}      (browser)
  {{\"kind\": \"url_changed\"}}                                   (browser)
  {{\"kind\": \"page_contains\", \"text\": \"<substring>\"}}      (browser)
  {{\"kind\": \"file_exists\", \"path\": \"<path>\", \"non_empty\": true}}

OUTPUT FORMAT
Return ONLY a JSON object of the form:
  {{\"plan\": [ {{step}}, {{step}}, ... ]}}
No prose. No markdown fences. No commentary.
"""


def _describe_catalogue(declarations: Iterable[dict | Any]) -> str:
    """Render the tool catalogue as a short string the planner can scan.
    Mirrors the one-line format used elsewhere so it stays cache-friendly."""
    lines: list[str] = []
    for d in declarations or ():
        try:
            name = d.get("name") if isinstance(d, dict) else getattr(d, "name", None)
            desc = (d.get("description") if isinstance(d, dict)
                    else getattr(d, "description", "")) or ""
        except Exception:
            continue
        if not name:
            continue
        desc = " ".join(str(desc).split())
        lines.append(f"- {name}: {desc[:160]}")
    return "\n".join(lines)


class PlanError(RuntimeError):
    pass


def _validate(plan: dict, tool_names: set[str]) -> list[dict]:
    if not isinstance(plan, dict) or "plan" not in plan:
        raise PlanError("plan missing 'plan' key")
    raw = plan["plan"]
    if not isinstance(raw, list) or not raw:
        raise PlanError("plan must be a non-empty list")
    if len(raw) > _MAX_STEPS:
        raise PlanError(f"plan too long ({len(raw)} > {_MAX_STEPS})")
    seen_ids: set[int] = set()
    cleaned: list[dict] = []
    for i, step in enumerate(raw, start=1):
        if not isinstance(step, dict):
            raise PlanError(f"step {i} is not an object")
        sid = int(step.get("id", i))
        if sid in seen_ids:
            raise PlanError(f"duplicate step id {sid}")
        seen_ids.add(sid)
        tool = str(step.get("tool", "")).strip()
        if tool != "speak" and tool not in tool_names:
            raise PlanError(f"step {sid} uses unknown tool {tool!r}")
        deps = step.get("deps") or []
        if not isinstance(deps, list):
            raise PlanError(f"step {sid} deps is not a list")
        for d in deps:
            if int(d) >= sid:
                raise PlanError(f"step {sid} depends on later/self id {d}")
            if int(d) not in seen_ids:
                raise PlanError(f"step {sid} depends on unknown id {d}")
        cleaned.append({
            "id":     sid,
            "verb":   str(step.get("verb", f"step {sid}"))[:80],
            "tool":   tool,
            "args":   dict(step.get("args") or {}),
            "deps":   [int(d) for d in deps],
            "verify": step.get("verify"),
        })
    return cleaned


def plan_goal(goal: str,
              declarations: Iterable[dict | Any],
              log: Callable[[str], None] | None = None) -> list[dict]:
    """Ask the model for a plan, validate it, return the cleaned steps.
    Raises PlanError on structural problems. Caller commits to the task store."""
    tool_names = set()
    for d in declarations or ():
        try:
            n = d.get("name") if isinstance(d, dict) else getattr(d, "name", None)
        except Exception:
            n = None
        if n:
            tool_names.add(n)

    system = _SYSTEM_PROMPT.format(max_steps=_MAX_STEPS)
    catalogue = _describe_catalogue(declarations)
    user = (
        f"TOOL CATALOGUE:\n{catalogue}\n\n"
        f"GOAL: {goal}\n\n"
        "Return only the JSON object."
    )

    if log:
        log(f"[Planner] planning goal: {goal[:80]}")

    # Short, cheap. Timeout bounded so the voice loop never stalls.
    raw = _gem.as_json(
        [system, user], tier=_gem.SMART, timeout_ms=30_000, default=None,
    )
    if raw is None:
        raise PlanError("planner returned nothing")
    return _validate(raw, tool_names)


def commit_plan(goal: str, steps: list[dict]) -> int:
    """Insert a plan into the task store, return the new task id."""
    task_id = _tasks.create_task(goal)
    # step id in plan → sqlite row id, so deps can be rewritten
    id_map: dict[int, int] = {}
    for s in steps:
        row_id = _tasks.add_step(
            task_id=task_id,
            idx=s["id"],
            verb=s["verb"],
            tool=s["tool"],
            args=s["args"],
            deps=[id_map[d] for d in s["deps"]],
            verify=s["verify"],
        )
        id_map[s["id"]] = row_id
    _tasks.set_task_status(task_id, "planned")
    return task_id


# ── Replanner ────────────────────────────────────────────────────────────────

_REPLAN_SYSTEM = """\
You are the replanning brain of a voice assistant. An earlier plan is partway
through. One step just failed in a way the arg-level repair pass could not
fix. Your job: return a NEW short plan for the REMAINING work only.

You will receive:
  - the original goal
  - the steps that already completed (and their short results)
  - the step that failed (with tool, args, and why it failed)

Rules:
  1. Return 1 to {max_steps} concrete, single-tool steps that finish the
     goal from where things stand. Never repeat work already marked done.
  2. If the failed step is retryable with a different approach (different
     tool, different args, different sequence), include that approach.
  3. If the failed step was non-essential, you may skip it and continue.
  4. If the goal is genuinely unreachable now, return a one-step plan using
     tool "speak" with an honest explanation of what could not be done.
  5. Every step must use a tool from the TOOL CATALOGUE. Same JSON shape as
     the forward planner: {{"id": int, "verb": str, "tool": str, "args": {{}},
     "deps": [ids], "verify": {{...}}|null}}. Dependencies reference only
     ids within this new plan, 1-based.
  6. Keep it short. Prefer fewer, bigger steps over many micro-steps.

Return ONLY a JSON object:
  {{"plan": [ {{step}}, ... ]}}
No prose, no markdown fences.
"""


def replan_from(task_id: int,
                goal: str,
                done_steps: list[dict],
                failed_step: dict,
                failure_reason: str,
                declarations: Iterable[dict | Any],
                log: Callable[[str], None] | None = None) -> list[dict]:
    """Produce an amended plan for the remaining work after a step failed.

    Returns cleaned step dicts in the same shape as `plan_goal`. Caller is
    responsible for splicing them into the task store (see tasks.add_step)
    and cancelling any now-stale planned steps."""
    tool_names: set[str] = set()
    for d in declarations or ():
        try:
            n = d.get("name") if isinstance(d, dict) else getattr(d, "name", None)
        except Exception:
            n = None
        if n:
            tool_names.add(n)

    system = _REPLAN_SYSTEM.format(max_steps=_MAX_STEPS)
    catalogue = _describe_catalogue(declarations)
    done_desc = "\n".join(
        f"- [{s.get('verb','?')}] tool={s.get('tool','?')} result={str(s.get('result',''))[:200]}"
        for s in done_steps
    ) or "(nothing finished yet)"
    user = (
        f"TOOL CATALOGUE:\n{catalogue}\n\n"
        f"ORIGINAL GOAL: {goal}\n\n"
        f"STEPS ALREADY DONE:\n{done_desc}\n\n"
        f"FAILED STEP:\n"
        f"  verb:   {failed_step.get('verb','?')}\n"
        f"  tool:   {failed_step.get('tool','?')}\n"
        f"  args:   {json.dumps(failed_step.get('args') or {})}\n"
        f"  reason: {failure_reason[:400]}\n\n"
        "Return only the JSON object for the new plan."
    )

    if log:
        log(f"[Replanner] task {task_id}: re-plan after '{failed_step.get('verb','?')}'")

    raw = _gem.as_json(
        [system, user], tier=_gem.SMART, timeout_ms=30_000, default=None,
    )
    if raw is None:
        raise PlanError("replanner returned nothing")
    return _validate(raw, tool_names)


def commit_replan(task_id: int, new_steps: list[dict]) -> list[int]:
    """Splice a replanner's output into an existing task.

    - Cancels all currently planned/ready steps on the task (they belonged to
      the stale plan).
    - Appends the new steps with fresh row ids; new-plan deps (1-based within
      the new plan) are rewritten to SQLite row ids.
    - Leaves done/failed/running/cancelled steps alone — they are history.
    Returns the list of new row ids in input order."""
    _tasks.cancel_pending_steps(task_id)
    start_idx = _tasks.next_step_idx(task_id)
    id_map: dict[int, int] = {}
    row_ids: list[int] = []
    for offset, s in enumerate(new_steps):
        row_id = _tasks.add_step(
            task_id=task_id,
            idx=start_idx + offset,
            verb=s["verb"],
            tool=s["tool"],
            args=s["args"],
            deps=[id_map[d] for d in s["deps"]],
            verify=s["verify"],
        )
        id_map[s["id"]] = row_id
        row_ids.append(row_id)
    # Task must stay 'running' so the worker picks the new steps up on the
    # next tick. _maybe_finalise_task will see the planned rows and wait.
    _tasks.set_task_status(task_id, "running")
    return row_ids
