# Jarvis One-Man-Army Orchestrator — Full Implementation Plan

> Status: **DRAFT for user approval.** No code has been written. This document is the single source of truth; nothing ships until the user approves this plan in full.

---

## Table of Contents

1. Context and Vision
2. Problem the Plan Solves
3. Guiding Principles
4. Glossary
5. High-Level Architecture
6. Component Inventory (Every New & Modified File)
7. Full Data Model (Every Table, Column, Index, Constraint)
8. Role System — Complete Library + Synthesis Mechanics
9. Workflow & Goal Lifecycle (State Machines)
10. Sub-Agent Model (State Machine, Concurrency, Isolation)
11. Agent Bus Protocol (Message Kinds, Payload Schemas, Blackboard)
12. Template Library (Full Seed Set + Schema)
13. KPI Resolver Library (Every Seed Resolver)
14. Evaluation Engine
15. Report Generator (Prompt Design, Rollup Math, Output Shape)
16. Approval & Safety Model
17. Memory & Namespacing Strategy
18. Scheduler Extensions
19. Dashboard Routes & UI Surface
20. Integration Touchpoints with Existing Modules (Exact API Calls)
21. Concurrency, Locks, Backpressure
22. Configuration Knobs (Env Vars, JSON, Defaults)
23. Migration & Rollout Plan
24. Three End-to-End Scenarios (Business / Personal / Research)
25. Failure Modes & Recovery
26. Test Matrix (Unit, Integration, Regression, Acceptance)
27. Observability (Logs, Metrics, Audit)
28. Risk Register
29. Rollback Plan
30. Operational Runbook
31. Phased Delivery Plan (Phase A → E with Per-Phase Deliverables)
32. Known Limitations (Honest)
33. Non-Goals
34. Future Extension Points
35. Open Questions
36. Full Domain-Gap Coverage (all 24 domains, every missing feature)
    - 36.1 Activation & Conversational UX
    - 36.2 Audio Capture, Processing & Recognition
    - 36.3 Speech Output
    - 36.7 Communications & Collaboration
    - 36.8 Desktop/OS & Device Control
    - 36.10 Smart Home & IoT
    - 36.11 Media & Entertainment
    - 36.12 Creative/Educational/Professional
    - 36.13 Health, Wellbeing & Accessibility
    - 36.14 Commerce & Finance
    - 36.15 Travel, Mobility & Automotive
    - 36.16 Enterprise / Contact Center
    - 36.17 Multimodal & Sensor Fusion
    - 36.18 Proactive / Ambient / Agentic
    - 36.20 Identity, Personalization & Memory
    - 36.21 Privacy, Security & Governance
    - 36.22 Reliability, Quality & Operations
    - 36.23 Deployment, Architecture & Hardware
    - 36.24 Emerging & Future
37. Extended Phased Delivery (Phases F–N)
38. Expanded Component Inventory for Domain-Gap Work
39. Hardware & Platform Dependencies
40. Expanded Risk Register for Domain Gaps

---

## 1. Context and Vision

Jarvis today is a capable monolithic voice assistant: 66 actions across 23 of 24 domains, auto-discovered tool registry, a goal→steps planner (≤24 steps), a `TaskWorker` async executor with built-in repair (×2) and replan (×2), 3-tier approval gates, a cron/interval/once scheduler, a journal + daily reflection loop, CRM/projects/commitments/finance stores, vector memory, FastAPI dashboard, and an MCP + plugin ecosystem with hot-registration.

What the user is asking for is **not** another feature — it is an **organizational layer** on top of the existing engine. The user's working metaphor is a real business:

> Planning → Task Delegation → Execution → Reporting → Evaluation.

But the user is explicit that Jarvis must **not** be hardcoded for business. It must handle:

- A business owner setting revenue goals.
- A student finishing a thesis.
- A job seeker running a job-hunt.
- A couple planning a wedding.
- A developer shipping a product.
- A patient managing a chronic illness.
- A researcher conducting multi-source deep research.
- Any goal, any domain, any workflow.

Jarvis must behave like a **one-man-army** — a single system that is CEO, manager, specialist workforce, evaluator, and coach all at once. It must adapt to unfamiliar domains without being reconfigured by the user. It must act like a disciplined, intelligent, human assistant.

**Intended outcome:** user states an outcome in plain language; Jarvis decomposes it into a weighted goal tree, synthesizes the roles needed, dispatches work (persona switches for cheap serial work, sub-agents for parallel / long / isolated work), monitors continuously, auto-reports, evaluates against targets, improves itself — all while keeping voice UX, approval gates, and the full domain action library intact.

---

## 2. Problem the Plan Solves

Today a user saying *"hit $1M sales next month"* produces a single `tasks` row routed through the planner once. There is:

- no concept of **sub-goals with weights**;
- no concept of **roles** beyond "the assistant";
- no **parallel specialist execution**;
- no **long-horizon workflow state** (a workflow is implicit in a single task row);
- no **periodic KPI evaluation** against targets;
- no **cross-domain daily/weekly/monthly reports**;
- no **self-improvement loop** that learns from a completed workflow and proposes plan adjustments.

The plan adds a **Supervisor + Dynamic Role + Workflow** layer that fills these gaps **without** breaking the existing voice front-end, approval gates, action library, plugin or MCP ecosystem.

---

## 3. Guiding Principles

- **Goal-driven, not template-driven.** Templates are accelerators. The engine must handle any goal even if no template matches.
- **Reuse before building.** Existing `planner`, `tasks`, `worker`, `scheduler`, `approval`, `confirm`, `journal`, `reflection`, `vectors`, `gemini`, `crm`, `projects`, `finance`, `commitments`, `webhooks`, `dashboard`, `killswitch`, `resource_gate` must be reused where they fit. Adds are surgical.
- **Safety first.** Supervisor and sub-agents cannot bypass LVL3 approval. Killswitch halts everything.
- **Dynamic roles.** Seed library gives examples. The LLM synthesizes new roles per workflow when none fit. Synthesized roles default to conservative autonomy and can be edited by the user.
- **In-process parallelism.** Sub-agents are asyncio tasks in the same Python process (shared SQLite via WAL, shared killswitch/approval). No IPC. No subprocess supervisor. Low overhead.
- **Deterministic persistence.** Every workflow / goal / role / evaluation / report is a durable row. A crash does not lose state.
- **Backward compatible.** Every new column defaults to NULL/`global`/`supervisor`. Every existing command works unchanged.
- **Observable.** Every supervisor decision, role synthesis, sub-agent lifecycle event, approval, replan, and evaluation goes into `journal` and `agent_bus` for review.
- **Honest reporting.** `COMPLETED / PARTIALLY COMPLETED / ATTEMPTED / BLOCKED / NOT PERFORMED` are the only outcome labels. Reports never claim success without verifiable evidence.

---

## 4. Glossary

| Term | Meaning |
|------|---------|
| **Supervisor** | Central orchestrator agent inside Jarvis. Receives user goals, decomposes them, synthesizes roles, dispatches work, monitors progress, triggers evaluations and reports. |
| **Workflow** | A user-declared outcome in motion. Lifecycle: `understand → plan → delegate → execute → monitor → report → evaluate → improve → close`. Persists until explicitly closed. |
| **Goal node** | One node in the goal hierarchy. Root = user's outcome. Children = sub-goals. Leaves become executed tasks. Has `target_metric`, `target_value`, `weight`, `deadline`, `assigned_role`. |
| **Role** | A persona + tool filter + memory namespace + autonomy level + KPI set. Can be seed (library) or synthesized (LLM). |
| **Persona mode** | The default execution mode: Supervisor plays the role inline, using the same Gemini session and `TaskWorker`. |
| **Sub-agent mode** | The escalation mode: Supervisor spawns an asyncio `SubAgent` with its own LLM REST session, inbox, memory namespace, journal tag. |
| **Agent bus** | SQLite-backed message table + blackboard enabling inter-agent communication. |
| **Evaluation** | Periodic KPI snapshot: target vs actual, variance, score, bonus eligibility. |
| **Report** | LLM-composed Markdown + JSON summary of a scope over a period. |
| **Template** | Reusable workflow blueprint: `{name, triggers, goal_tree, roles_hint, kpis, cadence}`. |
| **Blackboard** | Key/value store, scoped per workflow or global, where agents post findings for others to read. |
| **KPI resolver** | A named Python function that computes `current_value` for a metric by querying existing tables (tasks, finance, journal, projects, commitments, CRM). |

---

## 5. High-Level Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  USER (voice / dashboard / chat / webhook)                   │
└───────────────┬──────────────────────────────────────────────┘
                │ NL goal / command / approval / query
┌───────────────▼──────────────────────────────────────────────┐
│  SUPERVISOR   core/supervisor.py                             │
│    understand → template/decompose → synthesize roles        │
│    → delegate (persona | sub-agent) → monitor → re-plan      │
│    → evaluate → report → improve → close                     │
└──┬──────────────────┬────────────────────────────────────────┘
   │ persona switch   │ spawn sub-agent (asyncio task)
   │ (cheap, serial)  │ (parallel / long / isolated)
┌──▼───────────┐ ┌────▼──────────────┐
│ PERSONA RUN  │ │ SUB-AGENT (N≤4)   │
│ main loop +  │ │ core/subagent.py  │
│ role filter  │ │ own LLM REST sess │
│ on tools     │ │ own inbox+memory  │
│ on memory    │ │ own journal tag   │
└──┬───────────┘ └────┬──────────────┘
   │                  │ agent_bus messages + blackboard
   └────────┬─────────┘
            │
┌───────────▼──────────────────────────────────────────────────┐
│  EXECUTION ENGINE (existing — reused unchanged)              │
│  core/planner.py → core/tasks.py → core/worker.py            │
│  TaskWorker: dispatch → verify → repair×2 → replan×2         │
└───────────┬──────────────────────────────────────────────────┘
            │
┌───────────▼──────────────────────────────────────────────────┐
│  TOOLS  actions/* + plugins/* + mcp__* + inline              │
│  (unchanged — filtered per role at planner call time)        │
└──────────────────────────────────────────────────────────────┘

Sidecars:  scheduler • evaluation • report_gen • agent_bus • approval • killswitch • resource_gate
State:     goal_hierarchy.sqlite • evaluations.sqlite • reports.sqlite • agent_bus.sqlite
           + existing tasks.sqlite • crm.sqlite • projects.sqlite • commitments.sqlite • finance.sqlite • journal/
           + existing memory/vectors.sqlite • memory/long_term.json
```

---

## 6. Component Inventory

### 6.1 New files

| Path | Lines (est.) | Purpose |
|------|--------------|---------|
| `core/supervisor.py` | 400 | Supervisor loop. NL understand, template match, free-form decompose, role selection/synthesis, mode decision, monitor, re-plan, evaluation+report trigger, close-out. |
| `core/workflow.py` | 200 | Workflow CRUD. `create`, `pause`, `resume`, `cancel`, `close`, `status`, `list_active`, `summary`. |
| `core/goals.py` | 300 | Goal tree model. `create_goal`, `add_dependency`, `get_tree`, `ready_leaves`, `progress_pct` (weighted rollup), `at_risk`, `update_status`, `set_current_value`, `assign_role`, `assign_agent`. |
| `core/roles.py` | 300 | RoleRegistry. Load seed `roles/role_definitions.json` + synthesized. `get(name)`, `all()`, `synthesize(goal, context) → RoleSpec`, `save_synthesized(RoleSpec)`. Hot-reload. Tool-filter regex compile + cache. |
| `core/subagent.py` | 350 | `SubAgent` asyncio runtime. Owns: id, role, inbox, memory_namespace, journal tag. Methods: `start`, `stop`, `tick`, `receive_assign`, `receive_question`, `emit_report`, `_plan_and_execute_goal`. Spawned/supervised by Supervisor. |
| `core/agent_bus.py` | 250 | SQLite tables + API. `send(msg)`, `inbox(to, kind=None, status='pending')`, `mark_read`, `blackboard_put`, `blackboard_get`, `blackboard_scan`. |
| `core/evaluation.py` | 300 | Periodic KPI sampler. `snapshot(workflow_id, period)`, `roll_up(workflow_id, period)`, resolver registry, variance+score math, bonus-eligibility logic. |
| `core/report_gen.py` | 350 | Composer. `generate(scope, period) → ReportRow`. Pulls goal tree + evaluations + journal + domain aggregates; LLM-composes Markdown; stores row; optional PDF via existing `actions/pdf_gen.py`. |
| `core/templates.py` | 250 | Template library. `search(nl_goal) → [(TemplateMatch, score)]` (vector similarity). `apply(template, params, user_profile) → GoalTreeBlueprint`. `save_from_workflow(wf_id, name)`. |
| `core/kpi_resolvers.py` | 250 | Named KPI resolvers: tasks_completed_in_period, invoices_paid_sum, expenses_sum, leads_added, words_written, workouts_logged, reflections_written, events_attended, goals_completed, meetings_held, custom_sql. Registered by name. |
| `core/supervisor_prompt.py` | 150 | All LLM prompts (understand, decompose, role_synth, improve, report_compose). Isolated for easy iteration + versioning. |
| `dashboard/routes_workflow.py` | 300 | FastAPI router: `/api/workflows`, `/api/workflows/{id}`, `/api/goals/{id}`, `/api/goals/{id}/tree`, `/api/roles`, `/api/roles/{name}`, `/api/agents`, `/api/evaluations`, `/api/reports/{period}`, `/api/reports/{id}/pdf`. Reuses existing auth + rate-limit middleware. |
| `templates/workflow_templates/business-growth.json` | 80 | Seed template. |
| `templates/workflow_templates/product-launch.json` | 80 | Seed template. |
| `templates/workflow_templates/job-hunt.json` | 80 | Seed template. |
| `templates/workflow_templates/research-paper.json` | 80 | Seed template. |
| `templates/workflow_templates/fitness-program.json` | 80 | Seed template. |
| `templates/workflow_templates/learning-path.json` | 80 | Seed template. |
| `templates/workflow_templates/event-planning.json` | 80 | Seed template. |
| `templates/workflow_templates/personal-finance-reset.json` | 80 | Seed template. |
| `roles/role_definitions.json` | 300 | Seed role library — 10 archetypes, full prompts + tool regex. |
| `tests/test_goals.py` | 250 | Tree, rollup, deps, at_risk. |
| `tests/test_workflow.py` | 200 | Lifecycle. |
| `tests/test_supervisor.py` | 300 | Understand → decompose → delegate (mocked LLM). |
| `tests/test_roles.py` | 200 | Registry, synthesis validation, tool-filter compile. |
| `tests/test_subagent.py` | 250 | Spawn, tick, inbox, isolation, killswitch. |
| `tests/test_agent_bus.py` | 200 | Message round-trip, blackboard scope. |
| `tests/test_evaluation.py` | 250 | Resolvers, variance, score, bonus. |
| `tests/test_report_gen.py` | 200 | Composition, PDF path. |
| `tests/test_templates.py` | 200 | Search, apply, save. |
| `tests/integration/test_workflow_end_to_end.py` | 400 | Full lifecycle with LLM stubs. |

**Total new: ~24 files, ~6,000 lines (incl. tests).**

### 6.2 Modified files (surgical edits only)

| Path | Edit |
|------|------|
| `core/tasks.py` | Add 4 ALTER TABLE statements (idempotent, pattern already in `_init_schema`): `workflow_id`, `goal_id`, `role`, `agent_id`. Extend `create_task` signature with these optional kwargs. Add two indexes. Add `list_tasks_by_workflow`, `list_tasks_by_goal`. |
| `core/planner.py` | Add optional kwargs to `plan_goal` and `replan_from`: `role_persona: str`, `tool_filter: Callable[[str], bool]`. When supplied, filter declarations and prepend role persona to system prompt. Default behavior unchanged. |
| `core/worker.py` | On step done/failed, read `tasks.goal_id`; if non-null, call `core.goals.on_step_terminal(goal_id, step_status)` for rollup. Tag journal writes with `role`, `agent_id` from the task row. |
| `core/action_loader.py` | Add optional TOOL fields `tags: [str]`, `allowed_roles: [str]`, defaults `[]` and `'*'`. Registry gains `filter_for_role(role_name, role_spec) → subset`. Zero impact on existing actions that don't declare them. |
| `core/plugin_loader.py` | Same optional fields as above, same `filter_for_role` helper. |
| `core/approval.py` | Add optional `role: RoleSpec | None` arg to `classify()` and `gate()`. If role has `autonomy_level >= 2`, a LVL2 whose category is in `role.lvl2_auto_approved` auto-approves. LVL3 never auto-approves regardless of role. |
| `core/journal.py` | No signature change (already takes `**fields`). Add `role`, `agent_id` to the standard field conventions in the docstring. Dashboard filter honors them. |
| `core/vectors.py` | Add `namespace TEXT DEFAULT 'global'` column via idempotent ALTER. `search()` gains optional `namespace: str | None`; `None` = all namespaces. `embed_and_store()` gains `namespace='global'`. |
| `core/scheduler.py` | Add two new meta-field conventions (no DDL change — reuses `meta_json`): `meta.kind='evaluation'` fires `evaluation.snapshot(...)` via dispatcher table; `meta.kind='report'` fires `report_gen.generate(...)`. New dispatcher map `SCHEDULER_META_DISPATCH` wired at app start. |
| `core/reflection.py` | Add optional `role: str | None` and `scope: 'global' | 'workflow'` args. Lessons stored under `learned/<role or 'global'>/<date>` in `memory/long_term.json`. |
| `core/resource_gate.py` | Add `allow_subagent_spawn(active_count: int) -> bool`. Env-override thresholds. |
| `main.py` | Mount Supervisor loop alongside existing `JarvisLive`. On goal detected (new voice intent `start_workflow`), route to `supervisor.start_workflow()`. Persona switching injects `role.persona_prompt` into the Gemini Live turn. |
| `dashboard/server.py` | `app.include_router(routes_workflow.router)` after existing routes. |
| `memory/memory_manager.py` | Namespace-aware `save` / `recall` (optional `namespace` arg; defaults to `'global'`). Preserves existing API. |

**Total modifications: 14 files, ~500 lines added, zero deletions.**

### 6.3 Net footprint

| Category | Count |
|----------|-------|
| New Python modules | 11 |
| New JSON seeds | 9 (8 templates + 1 role library) |
| New test files | 10 |
| New dashboard route files | 1 |
| New SQLite DBs | 4 (`goal_hierarchy`, `evaluations`, `reports`, `agent_bus`) |
| Modified Python modules | 14 |
| Deletions | 0 |
| Breaking changes | 0 |

---

## 7. Full Data Model

### 7.1 `memory/goal_hierarchy.sqlite`

```sql
CREATE TABLE workflows (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  user_goal_text  TEXT    NOT NULL,                     -- raw NL from user
  title           TEXT,                                  -- short LLM-generated
  description     TEXT,                                  -- LLM-expanded brief
  status          TEXT    NOT NULL DEFAULT 'active',     -- active|paused|done|cancelled|failed
  root_goal_id    INTEGER,                               -- FK → goals.id
  template_id     TEXT,                                  -- source template name, or NULL
  cadence_json    TEXT    NOT NULL DEFAULT '{}',         -- {"evaluate":"daily","report":"weekly"}
  user_id         TEXT,                                  -- reserved for multi-user (NULL today)
  priority        INTEGER NOT NULL DEFAULT 2,            -- 0=critical … 4=low
  created_at      REAL    NOT NULL,
  updated_at      REAL    NOT NULL,
  closed_at       REAL,
  summary         TEXT                                   -- populated on close
);

CREATE INDEX idx_wf_status ON workflows(status);
CREATE INDEX idx_wf_updated ON workflows(updated_at);

CREATE TABLE goals (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  workflow_id        INTEGER NOT NULL,
  parent_id          INTEGER,                             -- NULL for root
  title              TEXT    NOT NULL,
  description        TEXT,
  target_metric      TEXT,                                -- e.g. 'sales_usd'
  target_value       REAL,
  current_value      REAL    NOT NULL DEFAULT 0,
  unit               TEXT,                                -- e.g. 'USD', 'words', 'reps'
  deadline           INTEGER,                             -- unix ts, nullable
  status             TEXT    NOT NULL DEFAULT 'planned',  -- planned|active|blocked|at_risk|done|cancelled|failed
  weight             REAL    NOT NULL DEFAULT 1.0,        -- contribution to parent progress
  assigned_role      TEXT,                                -- role name
  assigned_agent_id  TEXT,                                -- sub-agent id or NULL
  mode               TEXT,                                -- 'persona' | 'subagent'
  kpi_resolver       TEXT,                                -- name in kpi_resolvers registry (optional)
  kpi_params_json    TEXT    NOT NULL DEFAULT '{}',       -- resolver kwargs
  notes              TEXT,
  created_at         REAL    NOT NULL,
  updated_at         REAL    NOT NULL,
  FOREIGN KEY (workflow_id) REFERENCES workflows(id) ON DELETE CASCADE,
  FOREIGN KEY (parent_id)   REFERENCES goals(id)      ON DELETE CASCADE
);

CREATE INDEX idx_goals_wf     ON goals(workflow_id);
CREATE INDEX idx_goals_parent ON goals(parent_id);
CREATE INDEX idx_goals_status ON goals(status);

CREATE TABLE goal_dependencies (
  goal_id              INTEGER NOT NULL,
  depends_on_goal_id   INTEGER NOT NULL,
  PRIMARY KEY (goal_id, depends_on_goal_id),
  FOREIGN KEY (goal_id)            REFERENCES goals(id) ON DELETE CASCADE,
  FOREIGN KEY (depends_on_goal_id) REFERENCES goals(id) ON DELETE CASCADE
);
```

### 7.2 `memory/evaluations.sqlite`

```sql
CREATE TABLE evaluations (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  workflow_id     INTEGER NOT NULL,
  goal_id         INTEGER NOT NULL,
  role            TEXT,
  period          TEXT    NOT NULL,                      -- daily|weekly|monthly|adhoc
  period_start    INTEGER NOT NULL,
  period_end      INTEGER NOT NULL,
  metric          TEXT    NOT NULL,
  target_value    REAL,
  actual_value    REAL    NOT NULL,
  variance_pct    REAL,                                  -- (actual - target) / target * 100
  score           REAL    NOT NULL,                      -- 0.0 … 1.0
  bonus_eligible  INTEGER NOT NULL DEFAULT 0,            -- 0 | 1
  resolver        TEXT,                                  -- resolver name used
  notes           TEXT,
  created_at      REAL    NOT NULL
);

CREATE INDEX idx_ev_wf     ON evaluations(workflow_id);
CREATE INDEX idx_ev_goal   ON evaluations(goal_id);
CREATE INDEX idx_ev_period ON evaluations(period, period_start);
```

### 7.3 `memory/reports.sqlite`

```sql
CREATE TABLE reports (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  scope_type    TEXT    NOT NULL,                        -- workflow|goal|role|global
  scope_id      TEXT,                                     -- NULL for global
  period        TEXT    NOT NULL,                        -- daily|weekly|monthly|adhoc|retrospective
  period_start  INTEGER NOT NULL,
  period_end    INTEGER NOT NULL,
  title         TEXT,
  content_md    TEXT    NOT NULL,
  content_json  TEXT,                                     -- structured facts
  pdf_path      TEXT,                                     -- optional, resolved via pdf_gen
  voiced_at     REAL,                                     -- if TTS-played, when
  generated_at  REAL    NOT NULL
);

CREATE INDEX idx_rep_scope  ON reports(scope_type, scope_id);
CREATE INDEX idx_rep_period ON reports(period, period_start);
```

### 7.4 `memory/agent_bus.sqlite`

```sql
CREATE TABLE agent_messages (
  id                     INTEGER PRIMARY KEY AUTOINCREMENT,
  from_agent             TEXT    NOT NULL,                -- role name OR sub-agent id
  to_agent               TEXT    NOT NULL,                -- role name, sub-agent id, or '*' (broadcast)
  kind                   TEXT    NOT NULL,                -- assign|report|question|answer|handoff|done|blocked|info
  payload_json           TEXT    NOT NULL DEFAULT '{}',
  related_workflow_id    INTEGER,
  related_goal_id        INTEGER,
  related_task_id        INTEGER,
  parent_msg_id          INTEGER,                         -- for answer threading
  status                 TEXT    NOT NULL DEFAULT 'pending', -- pending|read|replied|archived
  ts                     REAL    NOT NULL,
  read_ts                REAL,
  FOREIGN KEY (parent_msg_id) REFERENCES agent_messages(id) ON DELETE SET NULL
);

CREATE INDEX idx_am_to_status ON agent_messages(to_agent, status);
CREATE INDEX idx_am_wf        ON agent_messages(related_workflow_id);
CREATE INDEX idx_am_ts        ON agent_messages(ts);

CREATE TABLE blackboard (
  key            TEXT    NOT NULL,
  scope          TEXT    NOT NULL,                        -- workflow_id as string, or 'global'
  role_scope     TEXT,                                     -- optional role filter
  value_json     TEXT    NOT NULL,
  updated_by     TEXT    NOT NULL,                        -- role name or agent id
  updated_at     REAL    NOT NULL,
  PRIMARY KEY (key, scope)
);

CREATE INDEX idx_bb_scope ON blackboard(scope);
```

### 7.5 Modifications to existing SQLite stores

#### `memory/tasks.sqlite`

Add four columns via idempotent `ALTER TABLE tasks ADD COLUMN` (SQLite swallows duplicate-column error — existing idiom):

```sql
ALTER TABLE tasks ADD COLUMN workflow_id INTEGER DEFAULT NULL;
ALTER TABLE tasks ADD COLUMN goal_id     INTEGER DEFAULT NULL;
ALTER TABLE tasks ADD COLUMN role        TEXT    DEFAULT NULL;
ALTER TABLE tasks ADD COLUMN agent_id    TEXT    DEFAULT NULL;
```

Plus two indexes:

```sql
CREATE INDEX IF NOT EXISTS idx_tasks_workflow ON tasks(workflow_id) WHERE workflow_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_tasks_goal     ON tasks(goal_id)     WHERE goal_id IS NOT NULL;
```

#### `memory/vectors.sqlite`

```sql
ALTER TABLE vectors ADD COLUMN namespace TEXT DEFAULT 'global';
CREATE INDEX IF NOT EXISTS idx_vectors_namespace ON vectors(namespace);
```

Backfill: `UPDATE vectors SET namespace='global' WHERE namespace IS NULL;` — idempotent.

### 7.6 No changes to other stores

`crm.sqlite`, `projects.sqlite`, `commitments.sqlite`, `finance.sqlite`, `payments.sqlite`, `todos.sqlite`, `travel.sqlite`, `predictions.sqlite` remain **untouched**. The evaluation and report layers *read* these stores via resolvers — they do not mutate them.

---

## 8. Role System

### 8.1 Role spec schema

```jsonc
{
  "name":               "string",
  "persona_prompt":     "string — injected as system-prompt prefix when this role plans/executes",
  "allowed_tools":      ["regex", ...],    // tool-name allowlist
  "denied_tools":       ["regex", ...],    // overrides allowed_tools
  "memory_namespace":   "string",          // for memory_manager + vectors
  "autonomy_level":     0 | 1 | 2 | 3,     // 0=read-only, 1=LVL1+ask, 2=LVL1+LVL2-cat, 3=rare
  "lvl2_auto_approved": ["email_draft", "calendar_write", "todo_write", ...],
  "kpis":               ["resolver_name", ...],
  "resource_budget":    { "max_llm_calls_per_hour": 60, "max_minutes_per_tick": 5 },
  "source":             "seed" | "synthesized",
  "synthesized_for":    workflow_id | null,
  "synthesized_at":     unix_ts | null,
  "created_at":         unix_ts,
  "updated_at":         unix_ts
}
```

### 8.2 Seed role library (`roles/role_definitions.json`)

Ten archetypes ship. Every goal in any workflow maps to at least one of these before synthesis is considered.

1. **Planner** — decomposes goals, estimates effort, identifies dependencies, assigns work. Tools: `web_search`, `research`, `calendar_manager`, `todo`, `project`, `forecast`, `decide`, `context_recall`, `save_memory`, `recall_memory`. Denied: `payment`, `shell`, `computer_settings`. Autonomy 2.
2. **Researcher** — gathers information from web, docs, APIs; synthesizes findings. Tools: `web_search`, `research`, `grounded`, `browser_control`, `browser_flow`, `ai_site`, `vision_analyze`, `screen_processor`, `pdf_gen`. Autonomy 2.
3. **Writer** — produces drafts: emails, posts, essays, scripts, specs. Tools: `code_helper`, `email_manager.draft`, `social_media.draft`, `pdf_gen`, `research`, `save_memory`. Denied: `email_manager.send`, `social_media.post`. Autonomy 2 (drafts), LVL3 on actual send.
4. **Communicator** — outbound comms: email send, DM, SMS, call. Tools: `email_manager`, `send_message`, `contacts_manager`, `social_media`. Autonomy 1 (asks for every send unless the specific recipient is pre-approved in profile).
5. **Analyst** — numeric + comparative work: finance, forecasts, dashboards, metrics. Tools: `finance`, `market_research`, `forecast`, `python_exec`, `code_helper`, `pdf_gen`. Autonomy 2.
6. **Builder** — produces software/code/configs. Tools: `code_helper`, `dev_agent`, `python_exec`, `shell`, `file_controller`. Autonomy 1 (shell + file-write confirm per session).
7. **Reviewer** — audits outputs for correctness, risk, policy. Tools: `code_helper`, `grounded`, `research`, `web_search`. Autonomy 2.
8. **Coach** — behavioral workflows: habits, fitness, learning, wellness. Tools: `habit`, `morning_briefing`, `reminder`, `notify`, `education`. Autonomy 2.
9. **Negotiator** — multi-party / adversarial coordination: scheduling across people, deal terms, vendor replies. Tools: `calendar_manager`, `email_manager.draft`, `send_message`, `contacts_manager`, `crm`. Autonomy 1.
10. **Gatekeeper** — triage + prioritization. Tools: `gatekeeper`, `crm`, `decide`, `notify`, `email_manager.read`. Autonomy 3 (read-only on inbound; never sends without LVL3 confirm).

### 8.3 Role synthesis

When Supervisor needs a role not in the library (e.g. `Thesis-Advisor`, `Grant-Writer`, `Fitness-Coach`, `Deal-Scout`, `Code-Tutor`, `PR-Pitcher`), it calls Gemini SMART with:

- the sub-goal description + target_metric + deadline;
- the full existing role library (so the LLM can combine or extend);
- the full tool catalogue (name + one-line description, same shape the planner receives);
- the user profile (so the role is tuned to the user);
- hard rules: autonomy_level defaults to 1, tool regex must compile, no tool in `_FORBIDDEN_DENY_LIST` (`payment`, `shell`, `computer_settings.shutdown`, `file_controller.delete`, etc. — these stay LVL3 regardless).

Response is validated (`roles.validate_role_spec`). If invalid, Supervisor falls back to the closest matching seed role.

Synthesized role is persisted to `roles/role_definitions.json` with `"source": "synthesized"` and surfaced on the dashboard for user review & edit.

### 8.4 Tool filtering

`core/action_loader.py` and `core/plugin_loader.py` each expose:

```python
def filter_for_role(role: RoleSpec) -> list[dict]:
    """Return the TOOL declarations that the role is allowed to use."""
```

Logic:

```
for tool in all_tools:
    name = tool["name"]
    if any(re.fullmatch(r, name) for r in role.denied_tools): continue
    if role.allowed_tools and not any(re.fullmatch(r, name) for r in role.allowed_tools): continue
    include(tool)
```

The filtered list is passed to `planner.plan_goal(..., declarations=filtered)` so the LLM only sees tools the role may use. No change to the planner's internal validation (it already enforces "no unknown tools").

---

## 9. Workflow & Goal Lifecycle

### 9.1 Workflow state machine

```
                   ┌──────┐
       create ─────▶ active ────┐
                   └─┬────┘    │ every leaf goal done
                     │         │
            pause    │         ▼
             │       │       done ──▶ (closed, summary written, templates offered)
             ▼       │         ▲
          paused  cancel       │
             │       │   root goal explicitly cancelled
             ▼       ▼         │
          resume → active      │
                     │         │
                     └─ failed ┘  (all replan attempts exhausted on blocking critical goal)
```

### 9.2 Goal state machine

```
planned ──assigned+ready──▶ active ──step-done──▶ done ──┐
   │                          │                         │
   │                          ├─step-failed-blocked──▶ blocked
   │                          │                         │
   │                          └─deadline-at-risk──▶ at_risk
   │                                                    │
   └────user-cancel/wf-cancel──▶ cancelled              ▼
                                                     failed (after replan exhausted)
```

Rollup: `progress_pct(goal)` =
- leaf: `done` → 1.0; `failed/cancelled` → 0.0 (counted against parent as zero-weight contribution); `at_risk` → use `current_value / target_value` if numeric, else 0.5; `active` → use `current_value / target_value` if numeric, else `step_done / step_total`; `blocked` → 0.0.
- branch: `sum(child.weight * child.progress_pct) / sum(child.weight)`.

### 9.3 Supervisor tick

Every `SUPERVISOR_TICK_SECONDS` (default 30 s, env override):

1. **Killswitch check.** If active → cancel all sub-agents, mark Supervisor idle, exit tick.
2. **Resource check.** Skip tick if `resource_gate.allow()` is False.
3. **Monitor loop.** For every active workflow:
   - Rollup every goal's `current_value` from leaf tasks and KPI resolvers.
   - Flag `at_risk` where `now > deadline - safety_margin and progress_pct < expected_progress_pct(linear_schedule)`.
   - For blocked goals, attempt `planner.replan_from(...)`.
4. **Delegate loop.** For every workflow:
   - `goals.ready_leaves()` → list.
   - For each leaf, decide persona vs sub-agent (criteria in §10.1).
   - Spawn or enqueue accordingly.
5. **Bus loop.** Drain `agent_bus.inbox(to='supervisor', status='pending')`. Handle each message (report → log + possibly re-plan; question → LLM-answer or escalate to user; done → mark leaf done; blocked → re-plan or escalate).
6. **Report trigger.** For every workflow whose `cadence` says a report is due, enqueue `report_gen.generate()`.
7. **Evaluation trigger.** For every workflow whose `cadence` says an evaluation is due, enqueue `evaluation.snapshot()`.
8. **Close check.** For workflows with root goal `done` → close (summary, offer template save, archive schedules).

Tick is O(active_workflows × active_goals_per_wf). With 5 active workflows × 20 goals = 100 ops per tick; fine at 30 s cadence.

---

## 10. Sub-Agent Model

### 10.1 Persona vs sub-agent decision

Supervisor picks **sub-agent** when **any** of these hold:

- Estimated duration of the goal > `SUB_AGENT_MIN_MINUTES` (default 30 min).
- Two or more sibling goals are ready in parallel and dependencies allow independent execution.
- Goal requires **isolated context** (e.g. deep research across many tabs; context pollution would confuse main Gemini Live).
- Role carries `resource_budget.dedicated: true`.

Otherwise: **persona mode** — Supervisor plays the role inline, `tasks.create_task(..., role=role.name, workflow_id=wf, goal_id=g)` and `TaskWorker` picks it up.

Resource gate check: `resource_gate.allow_subagent_spawn(len(active_subagents))` must return True. If False, Supervisor defers (leaf stays `planned`) and retries next tick.

### 10.2 Sub-agent state machine

```
spawn ─▶ idle ─receive assign─▶ running ─tool loop─▶ step-done ─▶ running
                                   │                                 │
                                   ├─blocked (confirm/missing-info)──▶ awaiting_user
                                   │                                 │
                                   ├─killswitch / stop───────────▶ stopped
                                   │
                                   └─all steps done, report emitted──▶ idle (next assign)
```

### 10.3 Sub-agent runtime (`core/subagent.py`)

```python
class SubAgent:
    def __init__(self, agent_id: str, role: RoleSpec, workflow_id: int,
                 goal_id: int, notify: Notifier):
        self.id = agent_id          # e.g. "sa-00001"
        self.role = role
        self.workflow_id = workflow_id
        self.goal_id = goal_id
        self.inbox_cursor = 0
        self._stop = asyncio.Event()

    async def start(self): ...       # creates asyncio.Task, registers in SupervisorRegistry
    async def stop(self): ...
    async def tick(self): ...        # one planning+execution loop iteration
    async def _plan_and_execute(self): ...
```

Each tick:

1. Check killswitch, stop event.
2. Drain inbox (`assign`, `question`, `cancel`).
3. If current goal `done` / `failed` / `cancelled` → emit `done`/`blocked` to Supervisor, go idle.
4. Otherwise: call `planner.plan_goal(goal.description, declarations=role.filter(tools), role_persona=role.persona_prompt)`.
5. `planner.commit_plan(goal.description, steps)` with `create_task` tagged `role=role.name, agent_id=self.id, workflow_id, goal_id`.
6. Wait on `TaskWorker` to complete all steps (subscribe to task state changes).
7. On terminal state, emit `report` on agent_bus with structured outcome.

### 10.4 Isolation guarantees

- **Memory namespace.** Reads/writes go to `vectors(namespace=role.memory_namespace)` and `memory_manager.save(namespace=role.memory_namespace)`.
- **LLM session.** Sub-agents use `gemini.as_json(..., tier=SMART)` REST calls. Gemini Live stays with Supervisor (singleton).
- **Journal tag.** Every `journal.write` from this sub-agent carries `role=role.name, agent_id=self.id`.
- **Approval.** `approval.gate(..., role=role)` — LVL3 still blocks regardless of autonomy.
- **Resource budget.** Each sub-agent's LLM-call counter is tracked; exceeding `role.resource_budget.max_llm_calls_per_hour` stops the agent and emits `blocked:resource_budget`.

### 10.5 Hard caps

- `JARVIS_MAX_SUBAGENTS` (default 4) — Supervisor refuses to spawn beyond this. Env-override.
- `JARVIS_SUBAGENT_IDLE_TIMEOUT_MINUTES` (default 120) — idle sub-agents get stopped and the leaf re-queued.

---

## 11. Agent Bus Protocol

### 11.1 Message kinds + payloads

| Kind | From | To | Payload | Meaning |
|------|------|----|---------|---------|
| `assign` | supervisor | role name or sub-agent id | `{goal_id, goal_text, role_name, deadline, context_ref}` | Give an agent a goal. |
| `report` | role / sub-agent | supervisor | `{goal_id, outcome: completed/partial/attempted/blocked, actual_value?, notes, artifacts: [path]}` | Status report at end of execution attempt. |
| `question` | role / sub-agent | supervisor | `{goal_id, question, options?: [string]}` | Agent needs human-level answer (unavailable field, choice, etc.). |
| `answer` | supervisor | role / sub-agent | `{parent_msg_id, answer}` | Reply to a `question`. |
| `handoff` | role / sub-agent | another role / sub-agent | `{goal_id, reason, shared_context: {...}}` | Pass work laterally when another role is better suited. Must be acknowledged by Supervisor. |
| `done` | role / sub-agent | supervisor | `{goal_id, outcome: completed, actual_value?, artifacts: [path]}` | Terminal success. |
| `blocked` | role / sub-agent | supervisor | `{goal_id, reason, needs: 'approval' / 'user_input' / 'external' / 'resource' / 'tool_missing'}` | Terminal non-success; cannot proceed autonomously. |
| `info` | any | any | `{topic, content}` | Non-actionable update. |

### 11.2 API surface (`core/agent_bus.py`)

```python
def connect(db_path: Path) -> None
def send(from_agent: str, to_agent: str, kind: str, payload: dict,
         workflow_id: int | None = None, goal_id: int | None = None,
         task_id: int | None = None, parent_msg_id: int | None = None) -> int
def inbox(to_agent: str, kind: str | None = None, status: str = "pending", limit: int = 50) -> list[dict]
def mark_read(message_id: int) -> None
def mark_replied(message_id: int, reply_id: int) -> None
def recent(workflow_id: int | None = None, limit: int = 100) -> list[dict]

def blackboard_put(key: str, value: dict, scope: str, by: str, role_scope: str | None = None) -> None
def blackboard_get(key: str, scope: str) -> dict | None
def blackboard_scan(scope: str, prefix: str = "") -> list[dict]
```

### 11.3 Threading / concurrency

- All writes via a module-level `threading.RLock` (same idiom as `tasks.py`).
- WAL mode enabled.
- Subscribers can register for `(to_agent, kind)` to get wake-nudges (same pattern as `tasks.subscribe`).

### 11.4 Supervisor message handling

Per tick, Supervisor:

1. `inbox('supervisor', 'report')` → update goal `current_value`, mark goal `done` if `outcome=completed`.
2. `inbox('supervisor', 'blocked')` → attempt `planner.replan_from(...)`; if `needs == 'user_input'` or `'approval'` → surface via dashboard + voice.
3. `inbox('supervisor', 'question')` → LLM-answer if answerable from profile + blackboard + journal, else escalate to user.
4. `inbox('supervisor', 'done')` → close the leaf, rollup parent.
5. Mark all processed messages `read`.

---

## 12. Template Library

### 12.1 Template schema

```jsonc
{
  "name":             "product-launch",
  "description":      "Launch a product with marketing, waitlist, payment, post-launch support.",
  "triggers":         ["launch", "release", "ship product", "go live"],
  "roles_hint":       ["Planner", "Writer", "Builder", "Communicator", "Analyst"],
  "kpis":             ["signups_count", "payments_count", "launch_day_complete"],
  "cadence":          {"evaluate": "daily", "report": "weekly"},
  "goal_tree": {
    "title": "Launch {product_name}",
    "target_metric": "payments_count",
    "target_value":  "{target_customers}",
    "unit":          "customers",
    "deadline_offset_days": "{launch_in_days}",
    "children": [
      {
        "title":         "Prepare product (website, pricing, payment)",
        "assigned_role": "Builder",
        "weight":        0.30,
        "children": [
          {"title": "Build pricing page", "weight": 0.4},
          {"title": "Integrate payment provider", "weight": 0.4},
          {"title": "Harden deploy pipeline", "weight": 0.2}
        ]
      },
      {
        "title":         "Pre-launch comms (waitlist, teaser, outreach)",
        "assigned_role": "Writer",
        "weight":        0.25,
        "children": [
          {"title": "Draft launch email sequence", "weight": 0.4},
          {"title": "Draft social campaign", "weight": 0.3},
          {"title": "Build waitlist landing", "weight": 0.3}
        ]
      },
      {
        "title":         "Launch day execution",
        "assigned_role": "Communicator",
        "weight":        0.20,
        "deps":          ["Prepare product", "Pre-launch comms"],
        "children": [
          {"title": "Publish launch posts", "weight": 0.5},
          {"title": "Send launch email", "weight": 0.5}
        ]
      },
      {
        "title":         "Post-launch monitoring + support",
        "assigned_role": "Analyst",
        "weight":        0.25,
        "deps":          ["Launch day execution"],
        "children": [
          {"title": "Monitor signups + payments daily", "weight": 0.4},
          {"title": "Respond to customer tickets", "weight": 0.4},
          {"title": "Weekly retro", "weight": 0.2}
        ]
      }
    ]
  }
}
```

### 12.2 Seed templates (eight)

1. **business-growth** — revenue/leads/customers over N weeks. Roles: Planner, Analyst, Communicator, Researcher.
2. **product-launch** — above.
3. **job-hunt** — resume → apply → interview → offer pipeline. Roles: Writer, Researcher, Negotiator, Communicator.
4. **research-paper** — question → literature → outline → draft → revise → submit. Roles: Researcher, Writer, Reviewer.
5. **fitness-program** — targets + habits + weekly check-ins. Roles: Coach, Analyst.
6. **learning-path** — curriculum + practice + quizzing + milestones. Roles: Coach, Reviewer.
7. **event-planning** — venue + invites + logistics + budget. Roles: Planner, Negotiator, Analyst, Communicator.
8. **personal-finance-reset** — budget + bills + subscriptions + emergency fund. Roles: Analyst, Planner.

### 12.3 Template matching

`templates.search(nl_goal)`:

1. Compute `vectors.embed(nl_goal)` (reuses `core/vectors.py` with `namespace='templates'`).
2. Compare to pre-embedded template descriptions + triggers (cached on first search).
3. Also apply keyword trigger match (substring — fast tie-breaker).
4. Return top-3 with score; apply threshold `TEMPLATE_MATCH_MIN_SCORE` (default 0.55).
5. Supervisor LLM-re-ranks the top-3 and picks one, or declines them all → free-form decomposition.

### 12.4 Template application

`templates.apply(template, params, user_profile)`:

- Substitute `{placeholders}` using `params` (filled by LLM from user utterance + profile).
- Compute concrete deadlines from `deadline_offset_days`.
- Instantiate goal tree + dependencies.
- Validate: every `assigned_role` exists in registry; every KPI resolver exists.
- Return `GoalTreeBlueprint` — Supervisor commits it via `workflow.create()` + `goals.create_goal(...)` bulk insert.

### 12.5 Saving a workflow as a new template

`templates.save_from_workflow(wf_id, name)`:

- Reads the final goal tree, the roles used, the achieved deadlines.
- Strips all per-user specifics (names, dollar amounts) → re-templatizes via LLM.
- Writes new `templates/workflow_templates/<name>.json`.
- Re-embeds description for search.

---

## 13. KPI Resolver Library

Resolvers are named functions. Each takes `(goal_row, params, period_start_ts, period_end_ts)` and returns a float.

### 13.1 Seed resolvers (`core/kpi_resolvers.py`)

| Name | Returns | Reads |
|------|---------|-------|
| `tasks_completed_in_period` | count of done tasks for this goal in window | `tasks.sqlite` |
| `tasks_completed_ratio` | done / total for this goal | `tasks.sqlite` |
| `invoices_paid_sum` | sum of invoices with status='paid' | `finance.sqlite` |
| `expenses_sum_by_category` | sum of expenses in category | `finance.sqlite` |
| `revenue_sum` | sum of invoices+receipts in period | `finance.sqlite` |
| `leads_added` | contacts with tier in (vip,investor,client) added in window | `crm.sqlite` |
| `meetings_held` | interactions kind='meeting' in window | `crm.sqlite` |
| `words_written` | sum of `words` field in journal `writer_output` events | `journal/` |
| `workouts_logged` | count of `habit_logged` with category='fitness' | `commitments.sqlite` + journal |
| `reflections_written` | count of `reflection_done` events | `journal/` |
| `events_attended` | calendar events ≤ now in window | integration via calendar_manager |
| `project_milestones_done` | milestones status='done' in window | `projects.sqlite` |
| `goal_descendants_done_ratio` | descendant leaves done / total | `goal_hierarchy.sqlite` |
| `custom_sql` | execute user-provided safe-read-only SQL | any read-only store |

### 13.2 Resolver registration + safety

- Resolvers registered at startup by name in a module-level dict.
- `custom_sql` validates query:
  - must start with `SELECT`;
  - no `;`;
  - no DML keywords (`INSERT`, `UPDATE`, `DELETE`, `ALTER`, `DROP`, `ATTACH`, `PRAGMA`);
  - executed with `PRAGMA query_only=ON` on a connection opened read-only.
- A role can declare its preferred resolvers in `role_definitions.json.kpis`.

### 13.3 Scoring

For each evaluation:

```
if target_value is None or target_value == 0:
    score = 1.0 if actual_value > 0 else 0.0
elif direction == 'higher_is_better':  # default
    score = max(0.0, min(1.0, actual_value / target_value))
elif direction == 'lower_is_better':
    score = max(0.0, min(1.0, target_value / max(actual_value, 1e-9)))

variance_pct = ((actual_value - target_value) / target_value * 100) if target_value else None
bonus_eligible = (score >= role.kpi_bonus_threshold or 0.9)  # defaults 0.9
```

---

## 14. Evaluation Engine

### 14.1 API

```python
# core/evaluation.py
def snapshot(workflow_id: int, period: str = 'adhoc',
             period_start: int | None = None, period_end: int | None = None) -> list[dict]
def roll_up(workflow_id: int, period: str) -> dict
def history(workflow_id: int | None = None, goal_id: int | None = None,
            period: str | None = None, limit: int = 100) -> list[dict]
```

### 14.2 Snapshot algorithm

1. Resolve `[period_start, period_end]` from `period` + now (`daily` = last 24 h; `weekly` = last 7 d; `monthly` = calendar month).
2. For every goal in workflow with a `target_metric` + `kpi_resolver`:
   - call `kpi_resolvers[goal.kpi_resolver](goal_row, params, start, end)`.
   - compute variance, score, bonus eligibility.
   - insert row in `evaluations`.
3. Rollup the whole workflow tree (weighted) and write a summary row with `goal_id = root_goal_id`.
4. Return the list of new rows.

### 14.3 Trigger paths

- **Scheduler.** `cadence.evaluate` → scheduler meta `{kind:'evaluation', workflow_id}`. Fires via existing `Scheduler.run`.
- **Supervisor tick.** On detecting a workflow transition (e.g. phase boundary), calls `snapshot(workflow_id, period='adhoc')`.
- **Explicit.** Dashboard/user can request an ad-hoc evaluation.

---

## 15. Report Generator

### 15.1 Trigger

- Scheduler meta `{kind:'report', scope_type, scope_id, period}` fired on cadence.
- Supervisor-triggered on workflow close (period = `retrospective`).

### 15.2 Composer flow

1. Gather inputs:
   - `workflow` row + `goals` tree (if workflow scope).
   - All `evaluations` for the period.
   - `journal` events in the period, filtered to this workflow / role / global.
   - Domain aggregates (finance, CRM, projects, commitments) for the scope.
   - Agent-bus messages in the period (reports, blocked, done).
   - User profile + preferences (tone, verbosity).
2. Build structured `report_facts` dict.
3. Call Gemini SMART with a compose prompt (see `supervisor_prompt.py`), returning Markdown.
4. Persist row to `reports`; optionally generate PDF via `actions/pdf_gen.py`.
5. Emit a dashboard WS notification + optional TTS speak-aloud at `cadence.voice_report_hour` if configured.

### 15.3 Report shape

Markdown body with H2 sections, in this order:

1. **Executive summary** — 3–5 sentences: outcome vs targets, headline wins, headline risks.
2. **Progress against goals** — per leaf: target / actual / variance / status.
3. **Role performance** — per role: assignments, success rate, KPI score.
4. **What went well** — concrete evidence from journal.
5. **What went wrong + why** — concrete evidence from `blocked` / replan events.
6. **Decisions made** — auto-approved mutations, LVL3 confirms.
7. **Proposed improvements** — LLM-derived, must reference concrete observations.
8. **Next period priorities** — ordered list.

Parallel JSON blob (`content_json`) carries the structured facts for machine consumption.

---

## 16. Approval & Safety Model

### 16.1 Levels (unchanged from existing `core/approval.py`)

- **LVL1** — read-only. Auto-approved always. Examples: web_search, vision_analyze, browser.get_*.
- **LVL2** — mutating but recoverable. Auto-approved per category if role allows. Examples: email_draft, calendar_write, form_submit (non-payment).
- **LVL3** — irreversible. **Always** requires explicit user confirm via `confirm.request`. Examples: payment, email_send, shell, file_delete, computer_settings.shutdown, social_media.post, browser.submit on checkout.

### 16.2 Role → LVL2 category mapping

A role's `lvl2_auto_approved` list names categories (not full tool names) it can run without asking. Example: `Writer` includes `email_draft, social_media_draft, pdf_write`; it does **not** include `email_send`.

LVL3 can **never** be bypassed by any role, regardless of autonomy_level. Synthesized roles may suggest LVL3 bypass — `roles.validate_role_spec()` strips those declarations.

### 16.3 Killswitch

- Supervisor polls `killswitch.is_active()` at the top of every tick.
- Sub-agents poll it on every tick and at every step boundary.
- When active: all sub-agent tasks cancelled, Supervisor stops dispatching, pending planned work stays persisted for resume.

### 16.4 Resource gate

- `resource_gate.allow()` — existing per-tick CPU/RAM gate.
- `resource_gate.allow_subagent_spawn(active_count)` — new; stricter thresholds (CPU<70, RAM<75, count < JARVIS_MAX_SUBAGENTS).

### 16.5 Prompt injection defense

- Agent bus messages are parsed as structured JSON only; no LLM prompt is ever built directly from raw message text without JSON extraction.
- Web-scraped content and email bodies read into a report go through `grounded` sanitization before prompt injection — existing pattern.
- Sub-agents cannot modify role definitions (`roles.save_synthesized` is Supervisor-only).

---

## 17. Memory & Namespacing

### 17.1 Namespaces

| Namespace | Owner | Lifetime | Use |
|-----------|-------|----------|-----|
| `global` | shared | permanent | user profile, preferences, long-term facts, saved memories |
| `workflow:<wf_id>` | workflow | until workflow close + N days | goal-tree context, blackboard summaries |
| `role:<role_name>` | role | permanent (retention policy) | lessons the role learned across workflows |
| `agent:<sa_id>` | sub-agent | until sub-agent stopped + N days | its own working context |
| `templates` | templates module | permanent | template description embeddings |

### 17.2 Retention

- `JARVIS_VECTOR_RETENTION_DAYS_WORKFLOW` default 90.
- `JARVIS_VECTOR_RETENTION_DAYS_AGENT` default 30.
- `JARVIS_VECTOR_RETENTION_DAYS_ROLE` default 180.
- `global` + `templates` not auto-pruned.
- Prune pass runs on reflection cadence.

### 17.3 Shared knowledge

- **Blackboard.** Short structured findings — JSON blobs keyed by topic — scoped per workflow. Everything readable by all roles in that workflow.
- **Long-term memory.** `memory_manager.save(category, key, value, namespace)` — role-specific lessons.
- **Journal.** Tagged with role + agent_id so reflection can distill per-role lessons.

---

## 18. Scheduler Extensions

No DDL change. The new behavior is encoded in `meta_json`:

```jsonc
// evaluation schedule
{"kind":"evaluation","workflow_id":42,"period":"daily"}
// report schedule
{"kind":"report","scope_type":"workflow","scope_id":"42","period":"weekly"}
// workflow-local reminder
{"kind":"workflow_nudge","workflow_id":42,"goal_id":117}
```

When Supervisor starts a workflow, it inserts the appropriate `scheduler.add_cron(...)` entries. When workflow closes, those schedules are removed.

Dispatcher: a small map `META_DISPATCH = {"evaluation": evaluation.snapshot, "report": report_gen.generate, "workflow_nudge": supervisor.nudge}`. The `_default_create_task` in `scheduler.py` is extended with a "dispatch" phase that inspects `meta.kind`; if recognized, calls the handler directly instead of creating a task.

---

## 19. Dashboard Routes & UI Surface

### 19.1 New routes (`dashboard/routes_workflow.py`)

| Method | Path | Returns |
|--------|------|---------|
| GET | `/api/workflows` | list of active + recently closed workflows |
| GET | `/api/workflows/{id}` | workflow detail incl. root goal |
| POST | `/api/workflows/{id}/pause` | pause |
| POST | `/api/workflows/{id}/resume` | resume |
| POST | `/api/workflows/{id}/cancel` | cancel |
| GET | `/api/workflows/{id}/tree` | full goal tree with progress |
| POST | `/api/workflows` | create workflow from NL goal (same path supervisor takes) |
| GET | `/api/goals/{id}` | one goal detail |
| POST | `/api/goals/{id}/update` | edit target/weight/deadline/role |
| GET | `/api/roles` | all roles (seed + synthesized) |
| GET | `/api/roles/{name}` | role detail |
| POST | `/api/roles/{name}` | create/update role (user edit) |
| GET | `/api/agents` | active sub-agents with inbox summaries |
| GET | `/api/evaluations` | query evaluations (filters: wf/goal/period) |
| GET | `/api/reports` | list reports |
| GET | `/api/reports/{id}` | one report (MD + JSON) |
| GET | `/api/reports/{id}/pdf` | PDF render (via existing pdf_gen) |
| GET | `/api/templates` | list templates |
| POST | `/api/templates` | save a workflow as template |
| WS | `/ws/workflows` | live push of workflow/goal/agent state changes |

### 19.2 UI additions (static HTML)

Already-existing dashboard is HTML+JS inline. Extend:

- **Workflows tab** — card per workflow with progress bar + goal tree collapsible.
- **Roles tab** — grid of role cards with persona prompt + tool filters + autonomy; "edit" and "view inbox".
- **Agents tab** — list of running sub-agents with stop button and inbox preview.
- **Evaluations tab** — chart per workflow (score over time) + table of latest.
- **Reports tab** — selector by period/scope; renders Markdown; PDF link.

All UI changes stay in `dashboard/static/` and reuse existing auth + crypto session.

---

## 20. Integration Touchpoints — Exact API Calls

### 20.1 Existing functions Supervisor and sub-agents will call

- `core.gemini.as_json(contents, tier=SMART, timeout_ms=30_000, default=None)` — all LLM work.
- `core.planner.plan_goal(goal, declarations, role_persona=None, tool_filter=None)` — plan a leaf goal.
- `core.planner.commit_plan(goal, steps)` — persist. **Will be modified** to accept `workflow_id, goal_id, role, agent_id` and forward to `tasks.create_task`.
- `core.planner.replan_from(task_id, goal, done_steps, failed_step, failure_reason, declarations)` — recovery.
- `core.tasks.create_task(goal, workflow_id=, goal_id=, role=, agent_id=)` — persist.
- `core.tasks.subscribe(cb)` — sub-agent wake on step completion.
- `core.tasks.task_steps(task_id)` — read step status.
- `core.worker.TaskWorker` — unchanged; already dispatches via injected `dispatch` callable.
- `core.scheduler.Scheduler.add_cron(name, cron_expr, goal, meta=)` — register evaluation/report schedules.
- `core.approval.gate(tool, args, title, detail, run, lvl2_category, role=)` — approvals.
- `core.confirm.request(key, title, detail, run)` — LVL3.
- `core.journal.write(event, **fields)` — logging with `role`/`agent_id`.
- `core.vectors.search(query, top_k=5, namespace=)` — read role/workflow memory.
- `core.vectors.embed_and_store(source, source_id, text, namespace=)` — write.
- `core.killswitch.is_active()` — before every tick/step.
- `core.resource_gate.allow() / allow_subagent_spawn(count)` — before dispatch/spawn.
- `core.reflection.run(role=, scope='workflow:<id>')` — improve-phase.
- `core.gatekeeper.classify(...)` — reused for inbound triage.
- `memory.memory_manager.save/recall(..., namespace=)` — long-term facts.

### 20.2 Existing action handlers Supervisor will exercise

All existing actions remain the only concrete execution surface. Supervisor never writes a new action; it only orchestrates the ones that exist (66 in `actions/`). The new `report_gen` optionally calls `actions/pdf_gen.py` for PDF rendering.

---

## 21. Concurrency, Locks, Backpressure

- **SQLite WAL** on every store — concurrent readers, single writer.
- **Module-level RLock** for each store wrapper (`tasks`, `goals`, `agent_bus`, `evaluations`, `reports`) matches existing idiom.
- **Supervisor + sub-agents + TaskWorker + scheduler** all run in the same asyncio loop.
- **Backpressure** — `TaskWorker._MAX_INFLIGHT = 3` is the floor; sub-agents enqueue via `tasks.create_task`, not by themselves running tool handlers, so step-level throughput stays bounded.
- **Rate limit** — Gemini cooldown ladder in `core/gemini.py` already paces REST calls across sub-agents.
- **Hard caps** — `JARVIS_MAX_SUBAGENTS` (default 4).

---

## 22. Configuration Knobs

All env vars prefixed `JARVIS_`.

| Var | Default | Meaning |
|-----|---------|---------|
| `JARVIS_SUPERVISOR_TICK_SECONDS` | 30 | Supervisor loop cadence. |
| `JARVIS_SUB_AGENT_TICK_SECONDS` | 10 | Sub-agent loop cadence. |
| `JARVIS_MAX_SUBAGENTS` | 4 | Hard cap. |
| `JARVIS_SUB_AGENT_MIN_MINUTES` | 30 | Duration threshold for persona→subagent decision. |
| `JARVIS_SUBAGENT_IDLE_TIMEOUT_MINUTES` | 120 | Idle stop. |
| `JARVIS_SUBAGENT_CPU_CEILING` | 70 | Spawn gate. |
| `JARVIS_SUBAGENT_RAM_CEILING` | 75 | Spawn gate. |
| `JARVIS_TEMPLATE_MATCH_MIN_SCORE` | 0.55 | Template similarity threshold. |
| `JARVIS_EVAL_DEFAULT_CADENCE` | daily | Default per new workflow. |
| `JARVIS_REPORT_DEFAULT_CADENCE` | weekly | Default per new workflow. |
| `JARVIS_VOICE_REPORT_HOUR` | 18 | Hour of day to speak daily report (0–23); empty disables. |
| `JARVIS_BONUS_THRESHOLD` | 0.9 | KPI score ≥ this → `bonus_eligible=1`. |
| `JARVIS_VECTOR_RETENTION_DAYS_WORKFLOW` | 90 | Prune. |
| `JARVIS_VECTOR_RETENTION_DAYS_AGENT` | 30 | Prune. |
| `JARVIS_VECTOR_RETENTION_DAYS_ROLE` | 180 | Prune. |
| `JARVIS_ROLE_SYNTH_ALLOWED` | 1 | Toggle role synthesis on/off. |
| `JARVIS_SUPERVISOR_ENABLED` | 1 | Master toggle (0 reverts Jarvis to pre-plan behavior). |

JSON config files:

- `roles/role_definitions.json` — role library.
- `templates/workflow_templates/*.json` — templates.
- `config/kpi_resolvers.json` — optional custom resolver mapping (name → module.func).

---

## 23. Migration & Rollout Plan

### 23.1 Migration order (startup)

1. `tasks.connect(db_path)` — existing; idempotent `ALTER TABLE` adds 4 new columns + indexes.
2. `vectors.connect(...)` — idempotent ALTER adds `namespace`; backfill.
3. `goal_hierarchy.connect(...)` — new; creates tables if missing.
4. `evaluations.connect(...)` — new.
5. `reports.connect(...)` — new.
6. `agent_bus.connect(...)` — new.
7. `roles.load()` — read seed + synthesized file.
8. `templates.load()` — read + embed templates.
9. `scheduler.connect(...)` — existing.
10. `supervisor.start()` — mounts tick loop.

All migrations idempotent. Re-running on an already-migrated DB is a no-op.

### 23.2 Backward compatibility

- Pre-existing tasks have `workflow_id=NULL` → ignored by Supervisor rollup paths.
- Pre-existing journal rows have no `role` field → treated as `role='supervisor'` on dashboard filters.
- Setting `JARVIS_SUPERVISOR_ENABLED=0` fully disables new behavior; voice front-end runs exactly as today.

### 23.3 Rollout stages per phase

Each phase is independently useful and shippable. See §31.

---

## 24. Three End-to-End Scenarios

### 24.1 Business scenario — "Hit $1M in sales next month"

1. **Understand.** Supervisor asks Gemini SMART to normalize: `target_metric=revenue_sum, target_value=1_000_000, unit=USD, deadline=now+30d`.
2. **Template match.** `business-growth` matches at 0.78 → applied.
3. **Goal tree.**
   - Root: `revenue_sum=1,000,000 USD by 2026-12-18` (weight: n/a)
   - A. `Lead gen: 500 qualified leads` (Role=Communicator, weight 0.35, KPI `leads_added`)
   - B. `Sales conversion: 100 deals closed` (Role=Negotiator, weight 0.45, KPI `invoices_paid_sum`)
   - C. `Operations: deliver product/service` (Role=Builder, weight 0.20, KPI `tasks_completed_in_period`)
4. **Role synthesis.** All three roles in seed library. No synthesis.
5. **Delegate.** A and C are independent → sub-agent mode (parallel). B depends on A → persona mode, kicks off when A has leads.
6. **Execute.** Communicator SA runs browser automation + email outreach; Builder SA runs operational tasks; Negotiator (persona) handles inbound replies via `email_manager.draft` + gatekeeper tiering.
7. **Monitor.** Supervisor tick checks progress every 30 s; `evaluation.snapshot(daily)` fires each night.
8. **Report.** Weekly report Monday 9 AM: target vs actual revenue + leads + closes + role performance.
9. **Evaluate.** Daily evaluations stored; at week 2 if variance < -30 % Supervisor re-plans — maybe add a Marketing sub-goal, re-weight.
10. **Improve.** End-of-month retrospective — propose template adjustments (e.g. add `lead_qualification_rate` as new KPI next time).
11. **Close.** Workflow closes; user offered to save a customized "sales-push-{quarter}" template.

### 24.2 Personal scenario — "Finish my master's thesis by April 15"

1. **Understand.** `target_metric=words_written, target_value=25_000, deadline=2027-04-15`.
2. **Template match.** `research-paper` matches 0.82.
3. **Goal tree.**
   - Root
   - A. `Literature review — 60 sources` (Role=Researcher, weight 0.20)
   - B. `Outline 5 chapters` (Role=Planner, weight 0.05, deps=[A])
   - C. `Draft chapters 1–5` (Role=Writer, weight 0.40, deps=[B])
   - D. `Peer review feedback incorporated` (Role=Reviewer, weight 0.15, deps=[C])
   - E. `Revise + submit` (Role=Writer, weight 0.20, deps=[D])
4. **Synthesis.** `Thesis-Advisor` role synthesized (combines Reviewer + Coach + Writer), allocated to light-touch weekly check-ins.
5. **Delegate.** A is big and parallelizable → sub-agent (research across 20 databases). B–E persona mode (serial).
6. **Monitor.** Weekly habit check-ins via Coach-synthesized role; report on Sunday.
7. **Evaluate.** Weekly words_written vs linear schedule; monthly chapter-completion rate.
8. **Improve.** If progress < 70 % of plan at week 4, Supervisor re-prioritizes and increases daily writing cadence suggestion.
9. **Close.** Submit → workflow closes → retrospective.

### 24.3 Research scenario — "Deep research: compare 3 EV battery chemistries on cost, safety, longevity"

1. **Understand.** Multi-dimensional research question; no explicit target_value. Supervisor sets `target_metric=sources_cited, target_value=30`.
2. **Template match.** `research-paper` matches 0.6 — borderline. Supervisor chooses free-form decomposition.
3. **Goal tree.**
   - Root
   - A. `NMC chemistry: cost + safety + longevity` (Role=Researcher sub-agent)
   - B. `LFP chemistry` (Researcher sub-agent — parallel)
   - C. `Solid-state chemistry` (Researcher sub-agent — parallel)
   - D. `Comparative synthesis + report` (Role=Analyst, deps=[A,B,C])
4. **Synthesis.** `Chemistry-Research-Specialist` role synthesized with `allowed_tools=[web_search, research, grounded, pdf_gen]`.
5. **Delegate.** Three parallel sub-agents (within cap). Each writes findings to `blackboard` scoped to this workflow.
6. **Monitor.** Supervisor reads blackboard each tick; when all three sub-agents emit `done`, kicks off Analyst persona.
7. **Report.** Single comparative PDF.
8. **Close.** Workflow closes with retrospective and template save prompt.

---

## 25. Failure Modes & Recovery

| Failure | Detector | Recovery |
|---------|----------|----------|
| Sub-agent crashes mid-tick | asyncio task `done()` with exception | Mark goal `blocked`; log; re-spawn once; if second crash → escalate `blocked:crash` to user. |
| LLM returns invalid plan JSON | `planner._validate` raises `PlanError` | Retry once with stricter prompt; if still invalid → fall back to a single `speak` step that reports the problem. |
| Tool handler raises | `_dispatch` exception | Existing repair×2 → replan×2 → `blocked`. Supervisor bus message. |
| Deadline passed with goal not done | Monitor loop | Mark `at_risk` → re-plan → if re-plan fails → `blocked` + user notify. |
| Role synthesis returns invalid regex | `roles.validate_role_spec` | Fall back to closest seed role; journal `role_synth_rejected`. |
| Sub-agent exceeds `resource_budget` | Per-agent LLM call counter | Stop sub-agent, mark goal `blocked:resource_budget`; Supervisor re-queues at next window. |
| Approval timeout (LVL3) | `confirm.request` user never responds within `JARVIS_CONFIRM_TIMEOUT_MINUTES` (default 60) | Goal becomes `blocked:awaiting_approval`; Supervisor notifies again at next report; does not auto-approve. |
| SQLite WAL contention | sqlite retries exhausted | Backoff + requeue tick; journal `sqlite_contention`. |
| Killswitch tripped | `killswitch.is_active()` | All sub-agents cancelled; Supervisor idles; pending state persists for resume. |
| Dashboard disconnected | WS client dead | Non-fatal; writes continue, UI resyncs on reconnect. |

---

## 26. Test Matrix

### 26.1 Unit

| Module | Coverage targets |
|--------|------------------|
| `goals.py` | create/get/tree/rollup weighted/rollup with failed child/dependencies ready-check/at_risk detection |
| `workflow.py` | create/pause/resume/cancel/close lifecycle; indexes |
| `roles.py` | load seed; synthesize happy path; synthesize invalid regex falls back; LVL3 bypass stripped; tool filter correctness |
| `agent_bus.py` | send/receive/mark_read/blackboard scope & role_scope filters |
| `templates.py` | search ranking; apply with placeholders; save_from_workflow |
| `kpi_resolvers.py` | each resolver against fixture DB |
| `evaluation.py` | snapshot numeric; rollup weighted; bonus eligibility boundaries |
| `report_gen.py` | input assembly on fixtures; prompt shape; PDF path optional |
| `subagent.py` | spawn/tick/stop; killswitch halts within one tick; resource_budget exhaustion |
| `supervisor_prompt.py` | prompts stable (snapshot tests) |
| `approval.py` extension | role-aware classification does not downgrade LVL3 |

### 26.2 Integration

- **End-to-end workflow (mocked LLM).** Give NL goal → decompose → tree created → persona-mode delegate → tasks run against stubbed dispatch → root rolls up → close.
- **Parallel sub-agents.** Two independent goals → two sub-agents run concurrently; no SQL race.
- **Replan on failure.** Seed a failing tool handler on the first leaf; verify replan path kicks in.
- **Approval gate honored.** Sub-agent tries LVL3 tool → `confirm.request` fires; test harness refuses → goal `blocked`.
- **Killswitch.** Active during a running workflow → all sub-agents halted in one tick; task state persists; after reset, workflow resumable.
- **Scheduler → evaluation.** Fire a scheduled evaluation; verify DB row + rollup.
- **Scheduler → report.** Fire a scheduled report; verify MD+JSON+optional PDF.
- **Template match.** Give a goal matching an existing template; verify tree instantiation.
- **Template save.** Run a workflow to done; call save_from_workflow; verify new template on disk and searchable.

### 26.3 Regression

- All existing 100+ tests still pass (new columns default safely, no signature breaks).
- `test_planner.py` — added role kwargs are optional, unused calls unchanged.
- `test_approval.py` — role-unaware calls unchanged.
- `test_dashboard_*` — existing routes unaffected.

### 26.4 Acceptance scenario (manual + scripted)

Run three concurrent workflows (business, fitness, research) with 7 simulated days of fake clock. Verify:
- No cross-talk between workflow memory namespaces.
- All LVL3 tool calls blocked on `confirm` (none auto-executed).
- Weekly reports distinguish all three.
- Retrospective proposes at least one actionable improvement per workflow.
- Killswitch halts all; resume picks up precisely.

---

## 27. Observability

- **Journal events added.**
  - `workflow_create`, `workflow_pause`, `workflow_resume`, `workflow_close`
  - `goal_create`, `goal_assign`, `goal_status_change`, `goal_at_risk`, `goal_done`
  - `role_synth_ok`, `role_synth_rejected`, `role_load`
  - `subagent_spawn`, `subagent_stop`, `subagent_tick_err`
  - `agent_msg_send`, `agent_msg_handled`
  - `evaluation_snapshot`, `evaluation_rollup`
  - `report_generated`
  - `replan_triggered`
- **Dashboard.** Workflow/role/agent/eval/report tabs. All WS-live-updated.
- **Metrics** (future): optional Prometheus exporter via existing `logging_setup`. Phase E.

---

## 28. Risk Register

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| LLM mis-synthesizes a role with over-broad tools | Medium | High | Deny-list + synthesis validator; conservative default autonomy_level=1; user review on dashboard |
| Sub-agent runaway spending LLM quota | Medium | Medium | `resource_budget` per role; cooldown ladder; `JARVIS_MAX_SUBAGENTS` cap |
| Approval banner lost in UI → stuck sub-agent | Low | Medium | `blocked:awaiting_approval` surfaces in daily report; re-notify on report cadence |
| SQLite write contention at scale | Low | Low | WAL + bounded concurrency; migration path to server DB documented but not needed today |
| Template drift (user edits break schema) | Low | Low | Schema validator runs on template load; invalid templates skipped w/ journal entry |
| Hallucinated goal decomposition | Medium | Medium | Mandatory verifier spec on every planned step; `grounded` citation in reports |
| User expects voice-interactive sub-agents | Medium | Low | Doc clearly: sub-agents communicate via Supervisor only; status via dashboard + daily speak |
| Prompt-injection via scraped sources | Medium | Medium | `grounded` sanitization; no raw HTML in prompts; structured JSON payloads on bus |
| Workflow bloat (hundreds of active workflows) | Low | Low | Dashboard archive view; close-prompt on dormant workflows after N days of no progress |

---

## 29. Rollback Plan

Every phase sits behind feature flag `JARVIS_SUPERVISOR_ENABLED` (default 1 after first ship; 0 before).

Rollback stages:

1. **Soft rollback** — set env var to 0; restart. Voice front-end runs exactly as today. All workflow state persists intact for a later re-enable.
2. **Hard rollback** — git revert the feature commits; DBs remain with extra columns and tables but are ignored by the pre-change code. No data destruction.

---

## 30. Operational Runbook

- **Start/stop.** `python main.py` as today. Supervisor loop auto-starts if flag set.
- **Pause everything.** Trigger killswitch via existing CLI/hotkey.
- **Resume workflow.** Dashboard → Workflows → Resume (or `POST /api/workflows/{id}/resume`).
- **Inspect a stuck sub-agent.** Dashboard → Agents → view inbox.
- **Force evaluation.** `POST /api/workflows/{id}/evaluate` (ad-hoc).
- **Force report.** `POST /api/workflows/{id}/report?period=weekly`.
- **Prune memory.** `python -m core.vectors --prune` (reads retention env).
- **Edit a role.** Dashboard → Roles → Edit (writes to `roles/role_definitions.json`).
- **Edit a KPI resolver.** Edit `core/kpi_resolvers.py` or add a `custom_sql` entry in a goal row.

---

## 31. Phased Delivery Plan

Each phase is **independently useful** and **independently shippable**.

### Phase A — Foundation

**Files:** `core/goals.py`, `core/workflow.py`, `core/supervisor.py` (skeleton — persona mode only), `core/supervisor_prompt.py`, modifications to `core/tasks.py` (+4 cols) and `core/planner.py` (+ optional kwargs), new routes `dashboard/routes_workflow.py` (workflow + tree only).

**Deliverable:** User states any NL goal → Supervisor decomposes → goal tree stored → leaves execute serially via existing TaskWorker → dashboard renders tree with progress bars.

**Tests:** `test_goals.py`, `test_workflow.py`, `test_supervisor.py` (LLM stubbed).

**Verification:**
- `pytest -q tests/test_goals.py tests/test_workflow.py tests/test_supervisor.py`
- Launch: give `"write a 3000-word article on Mediterranean diets by Friday"` → tree + 4–6 leaves → runs to `done`.
- All existing tests still pass.

### Phase B — Roles + Templates

**Files:** `core/roles.py`, `roles/role_definitions.json`, `core/templates.py`, 8 seed templates, modifications to `action_loader`/`plugin_loader` (role filter), `main.py` persona switch injection, `core/approval.py` role-aware classify.

**Deliverable:** Supervisor picks a role per sub-goal (library or synthesizes), template matcher accelerates common flows, user can save a template.

**Tests:** `test_roles.py`, `test_templates.py` + supervisor integration with role-filtered planner.

**Verification:**
- `"launch my newsletter next month"` → template `product-launch` matched, roles assigned, each leaf planned against a filtered tool set.
- Synthesize a `Thesis-Advisor` on demand; verify regex compiles and no LVL3 tools leak in.

### Phase C — Sub-agents + Agent Bus

**Files:** `core/subagent.py`, `core/agent_bus.py`, Supervisor spawn logic, dashboard `/api/agents` + Agents tab.

**Deliverable:** Independent sub-goals run as parallel sub-agents; inter-agent messaging visible on dashboard; killswitch halts within one tick.

**Tests:** `test_subagent.py`, `test_agent_bus.py`, integration parallel-sub-agent test.

**Verification:**
- Two independent goals → two sub-agents complete in parallel observable by concurrent step timestamps in journal.
- Killswitch during run → both halted in ≤ one tick; state persists.

### Phase D — Evaluation + Reporting

**Files:** `core/evaluation.py`, `core/kpi_resolvers.py`, `core/report_gen.py`, scheduler meta-dispatch, dashboard `/api/evaluations` + `/api/reports` + tabs.

**Deliverable:** Daily/weekly/monthly reports, KPI charts, goals show progress against target, bonus eligibility surfaces.

**Tests:** `test_evaluation.py`, `test_report_gen.py`, scheduler integration test.

**Verification:**
- 3-day fake-clock workflow → daily evaluations stored → weekly report composed; numbers match independent SQL checks; PDF renders via `pdf_gen`.

### Phase E — Polish

**Files:** per-role autonomy UI, memory retention prune pass, workflow retrospective, user-editable role definitions UI, extra templates, docs, full regression sweep.

**Deliverable:** Shippable; 3-workflow concurrent acceptance scenario passes.

**Tests:** Acceptance scenario (§24) scripted + manual.

---

## 32. Known Limitations (Honest)

1. **LLM throughput.** Single Gemini key paces all sub-agents; ~15–30 LLM-heavy tool calls/min under 4 parallel sub-agents.
2. **Gemini Live singleton.** Only Supervisor uses Live (for voice). Sub-agents use REST; higher latency, no streaming.
3. **In-process sub-agents.** Chosen for low overhead; a crash in a sub-agent is contained via asyncio task boundaries but not full process isolation.
4. **SQLite single-writer.** Fine at expected scale; server-DB migration documented but not required.
5. **LVL3 confirm blocks the caller.** Sub-agent continues ticking other goals while one awaits approval.
6. **Synthesized role quality.** Only as good as the LLM; default to `autonomy_level=1`; user edits on dashboard; forbidden-tool deny-list enforced.
7. **Template matching.** Starts narrow (8 seeds); improves as users save successful runs; free-form decomposition is the safe fallback.
8. **Memory bloat.** Mitigated by retention policy; `global` namespace exempt.
9. **Local-only state.** All SQLite; user-driven backup.
10. **No multi-user isolation.** `user_id` column reserved but not used; add later.
11. **KPI resolvers.** Seed library covers ~14 patterns; many user metrics will need a `custom_sql` entry.
12. **Non-English locales.** Planner + role synthesis quality depends on Gemini multilingual performance.

---

## 33. Non-Goals

- Not replacing the voice assistant front-end — it layers on top.
- Not removing any action, plugin, MCP binding, or dashboard route.
- Not adding a new LLM provider — reuses Gemini + multi-backend ladder.
- Not changing the approval policy (LVL3 remains confirm-required).
- Not shipping industry-specific pre-built workflows beyond the 8 seeds; engine is goal-driven first.
- Not building a visual goal-tree editor beyond read + simple inline edits.
- Not emitting OpenTelemetry metrics in Phases A–E (added in Phase L).
- **All other PROJECT_OVERVIEW.md gaps are now in-scope** — see §36.

---

## 34. Future Extension Points

- **Household / guest mode.** Add `user_id` plumbing; tie into existing CRM tier.
- **Cloud backup.** Encrypted replication of SQLite stores to user-specified bucket.
- **True process isolation.** Move high-risk sub-agents (Builder with shell access) to subprocess with its own venv.
- **External specialist agents.** Replace a sub-agent role with an MCP server or remote API (swap at the role-definition level).
- **Marketplace of templates.** Export/import template packs between users.
- **Visual goal-tree editor.** Drag/drop reweighting + replanning.
- **Learning feedback.** Reflection output trains a per-user prompt prefix that improves planner quality.
- **Real-time voice sub-agent chat.** When Gemini Live supports multi-session, surface each sub-agent on a dedicated voice channel.

---

## 35. Open Questions

None blocking. Possible later discussions:

- Should evaluation+report cadences be user-adjustable per workflow at creation time via voice? (Trivial to add; default cadence usually suffices.)
- Should Supervisor expose a "dry run" mode where it prints the full plan before executing? (Useful for high-stakes workflows; one env flag.)
- Should reports be filterable/queryable (SQL-like) from voice? (Phase E if desired.)

---

## Definition of Done

This plan is approved when the user confirms, in writing, that:

- The scope, phases, and non-goals match their intent.
- The data model and role library are acceptable.
- The approval/safety model is sufficient.
- The phased delivery order is agreed.

After approval, implementation starts with **Phase A only**. Phases B–N are re-confirmed one at a time.

---

## 36. Full Domain-Gap Coverage

This section addresses every **Partial** and **Missing** feature from PROJECT_OVERVIEW.md §Implementation Status. Each domain has a per-feature micro-plan: what it does, which files to add or modify, data to persist, dependencies, risks, and the delivery phase it lands in (see §37 for the extended phase schedule).

Pattern across every domain: add a dedicated action/plugin module (auto-discovered by existing `ActionRegistry` / `PluginRegistry`), declare a `TOOL` dict, register KPI resolvers if measurable, expose dashboard views, route LVL3 operations through existing `approval.gate()`. No deletions, no breaking changes.

---

### 36.1 Activation & Conversational UX

#### 36.1.1 Visual / Gesture Activation (VA-UX-003)
- **Approach.** Reuse existing camera pipeline (`actions/vision_analyze.py`, `actions/screen_processor.py`). Add `plugins/wake_gesture.py` running a MediaPipe Hands lightweight detector on a 4 fps camera stream.
- **Files.** `plugins/wake_gesture.py` (new), optional `config/gesture_profiles.json` (user-tunable gestures).
- **Behavior.** Detected gesture (hand-wave, open-palm hold) raises the same wake-event the hot-word detector raises; user-configurable per gesture.
- **Permissions.** Explicit opt-in dashboard toggle; LED indicator while camera active.
- **Phase.** F (multimodal foundation).

#### 36.1.2 Whisper Mode (VA-UX-019)
- **Approach.** Measure input RMS + spectral centroid in `core/stt.py`. If below `WHISPER_THRESHOLD_DB` for >700 ms AND user utterance passes ASR, flip a session flag `whisper=True`. TTS layer (`core/tts.py`) reads the flag and applies `volume_reduction_db` + `pitch_shift_down`.
- **Files.** `core/stt.py` (extend), `core/tts.py` (extend), env `JARVIS_WHISPER_THRESHOLD_DB` default -40.
- **Phase.** F.

#### 36.1.3 Full-duplex / Barge-In (VA-UX-007, VA-UX-008)
- **Approach.** Already partial via Gemini Live streaming. Add an echo-cancellation pre-filter + VAD-driven interrupt: when ASR detects user speech during TTS playback, cut `audio_out` buffer, mark the in-flight response as interrupted, cancel the current tool chain cleanly (`asyncio.CancelledError` propagated through `TaskWorker._run_step`).
- **Files.** `core/tts.py` (add interruptible playback), `core/echo.py` (extend), `core/worker.py` (surface cancellation reason to replan path).
- **Risk.** Spurious interrupts on noisy input → debounce with 250 ms VAD confidence window.
- **Phase.** F.

#### 36.1.4 Mid-Conversation Language Switching (VA-UX-014)
- **Approach.** Add per-utterance language detection in `core/stt.py` (reuse Gemini Live transcription metadata; fallback to `langid.py`). On change, flip TTS voice to the matching language, re-prompt planner with locale-aware system prompt addendum.
- **Files.** `core/stt.py` (extend), `core/tts.py` (per-language voice registry), `core/locale.py` (new — language→locale→timezone→date-format).
- **Phase.** F.

---

### 36.2 Audio Capture, Processing & Recognition

#### 36.2.1 Sound / Event Recognition (VA-AUD-014)
- **Approach.** On-device lightweight model (YAMNet TFLite or Google AudioSet-trained MobileNet) listens on a background stream (same input device). Classifies non-speech events (doorbell, dog bark, smoke alarm, glass break, baby cry).
- **Files.** `plugins/sound_events.py` (new) with `TOOL = {name:'sound_events_listener', behavior:'NON_BLOCKING', scheduling:'WHEN_IDLE'}`. `assets/yamnet.tflite` bundled or user-downloaded on first run.
- **Integration.** Event raises a Supervisor `info` bus message with `{event, confidence, ts}`. If `confidence > 0.8` and `event in CRITICAL_EVENTS`, Supervisor speaks an alert.
- **Risk.** False positives — tunable threshold; `CRITICAL_EVENTS` user-editable.
- **Phase.** G (audio deep dive).

#### 36.2.2 Multi-party Recording / Meeting Capture (VA-AUD-015)
- **Approach.** `actions/meeting_recorder.py` — starts a recording session, writes WAV+MP3 to `~/Documents/JARVIS Meetings/`, on stop runs batched ASR (Gemini REST) + speaker diarization via `pyannote-audio` embeddings. Produces a transcript with `[Speaker A]`, `[Speaker B]` tags + LLM-summarized minutes.
- **Files.** `actions/meeting_recorder.py` (new), `memory/meetings.sqlite` (table: `meetings(id, title, started_at, ended_at, path_wav, path_mp3, transcript_path, summary_md, speaker_count)`).
- **Permissions.** Explicit "recording active" dashboard banner + optional audible chime every 5 min (jurisdiction-dependent).
- **Phase.** G.

#### 36.2.3 Speaker Diarization (VA-AUD-008)
- **Approach.** Reuse the `pyannote-audio` embeddings from §36.2.2. Expose as a standalone `actions/diarize.py` that accepts any WAV file and returns speaker-tagged segments.
- **Phase.** G.

---

### 36.3 Speech Output

#### 36.3.1 Spatial / 3D Audio (VA-SPO-009)
- **Approach.** For headphone output only, apply HRTF convolution on TTS audio using `pyroomacoustics` or a tiny built-in HRTF LUT. Position emitted by Supervisor (e.g. sub-agent "voice" placed off-center on the stereo field).
- **Files.** `core/tts.py` (extend with `position=(azimuth, elevation)` kwarg), `assets/hrtf_kemar.wav` (public-domain HRTF dataset).
- **Phase.** H.

#### 36.3.2 Voice Cloning (VA-SPO-011)
- **Approach.** Explicit opt-in. User records a 30-second consent + sample phrase. Jarvis stores the embedding (`coqui-TTS` or OpenAI TTS-with-voice API if licensed). Clone usage is LVL3-gated — every TTS call that uses a non-default voice must be approved once per session.
- **Files.** `actions/voice_clone.py`, `memory/voices.sqlite` (table: `voices(id, name, consent_recorded_at, embedding BLOB, source, created_at)`).
- **Safety.** Store only user's own voice; refuse to clone from arbitrary audio uploads (`actions.voice_clone.enroll` rejects a WAV unless a consent phrase is spoken in it).
- **Phase.** H.

#### 36.3.3 SSML & Speech Markup (VA-SPO-006)
- **Approach.** `core/tts.py` gains an `ssml: bool` kwarg; when True, input is validated against a tiny SSML subset (`<break>`, `<emphasis>`, `<prosody>`, `<say-as>`). Pass through to the backend TTS that supports it; strip and apply best-effort if not.
- **Phase.** F.

#### 36.3.4 Voice Authentication Watermarking (VA-SPO-012)
- **Approach.** Embed an inaudible high-frequency chirp (18–20 kHz, low amplitude) in every TTS output carrying a rolling HMAC token. A "did-Jarvis-say-this" verifier reads back a recorded audio to prove provenance. Jurisdiction-dependent — off by default.
- **Files.** `core/tts.py` (watermark encoder), `actions/voice_verify.py` (decoder + verifier).
- **Phase.** L (safety hardening).

---

### 36.7 Communications & Collaboration

#### 36.7.1 Voicemail Transcription (VA-COM-003)
- **Approach.** `actions/voicemail.py` reads Apple Voice Memos + macOS Phone voicemail folder (platform-specific paths), runs Gemini transcription, stores in `memory/voicemails.sqlite` (`voicemails(id, source, received_at, caller, duration_s, path, transcript, summary, priority)`). Hooks `gatekeeper.classify` for priority.
- **Phase.** F.

#### 36.7.2 Video Conferencing Voice Control (VA-COM-007)
- **Approach.** Zoom/Meet/Teams via their keyboard-shortcut APIs (macOS Accessibility). `actions/videoconf.py` with commands `mute/unmute`, `camera_on/off`, `raise_hand`, `start_recording`, `share_screen`, `leave_meeting`.
- **Files.** `actions/videoconf.py` (per-platform dispatchers), `config/videoconf_hotkeys.json` (user-editable).
- **Phase.** F.

#### 36.7.3 Intercom / Announcement Broadcast (VA-COM-008)
- **Approach.** Push an audio message to all registered Jarvis endpoints (phone + desktop + optional Home Assistant media_player entities via MCP). Requires device-registration table.
- **Files.** `actions/announce.py`, `memory/endpoints.sqlite` (`endpoints(id, name, type, address, last_seen)`).
- **Phase.** I (smart home layer).

#### 36.7.4 Spam / Consent Controls (VA-COM-013)
- **Approach.** For inbound (calls/SMS/email), cross-check caller against `crm.contacts`. Unknown + spam-pattern sender → auto-decline or divert to voicemail. Pattern DB: regex rules + optional user-trained bayesian classifier on past spam.
- **Files.** `actions/spam_filter.py`, `memory/spam.sqlite` (`spam_patterns(id, kind, pattern, source, created_at)`, `spam_decisions(id, source, decision, ts)`).
- **Phase.** F.

---

### 36.8 Desktop / OS & Device Control

#### 36.8.1 Peripheral Management (Printer, Scanner, Webcam) (VA-DEV-008)
- **Approach.** Printer: use CUPS on macOS/Linux, Windows print spooler via `win32print`. Scanner: `pyinsane2` where available. Webcam enumeration via `opencv`. All read-only enumerate + print_file + scan_to_file. Destructive ops (delete queue) are LVL3.
- **Files.** `actions/peripherals.py`.
- **Phase.** G.

#### 36.8.2 Remote Desktop Control (VA-DEV-011)
- **Approach.** Explicit opt-in. SSH outbound only (no inbound listener). User enrolls hosts in `config/remote_hosts.json` with key-based auth. `actions/remote_shell.py` runs commands on named hosts; all LVL3.
- **Files.** `actions/remote_shell.py`, `config/remote_hosts.json`.
- **Risk.** High — restricted to named hosts; no wildcard match; commands length-limited; audit log.
- **Phase.** L.

#### 36.8.3 Voice-Driven Pointer / GUI Control (VA-DEV-005)
- **Approach.** macOS Accessibility + `pyautogui` for cross-platform. Use screen OCR (`screen_processor.py` already exists) to resolve "click the blue Submit button" to coordinates. Each click is LVL2 (potentially destructive).
- **Files.** `actions/gui_click.py`, `actions/gui_describe.py` (list clickable elements on screen).
- **Phase.** G.

---

### 36.10 Smart Home & IoT

This entire domain is **zero-implementation** today. Chosen approach: **Home Assistant (HA) MCP bridge** — do not reinvent device drivers, delegate to HA which already supports Matter, Z-Wave, Zigbee, Hue, Nest, TP-Link, etc. Jarvis talks to HA over its REST+WebSocket API via an MCP server.

#### 36.10.1 HA Bridge
- **Files.** `plugins/homeassistant.py` (plugin) or dedicated MCP server at `mcp_servers/homeassistant/`. Config: `config/homeassistant.json` with `{base_url, token}`.
- **Discovery.** On startup, `GET /api/states` enumerates entities → auto-register per-entity tools: `light.living_room.turn_on`, `climate.thermostat.set_temp`, etc.
- **Scenes/Routines.** Mirror HA scripts as `scene.<name>.activate` tools.
- **Phase.** I.

#### 36.10.2 Lighting / Plugs / Switches (VA-IOT-001) — via HA.
#### 36.10.3 HVAC / Thermostats / Fans (VA-IOT-002) — via HA.
#### 36.10.4 Blinds / Shades (VA-IOT-003) — via HA.
#### 36.10.5 Locks / Doors / Garage (VA-IOT-004) — via HA; LVL3 on unlock operations; voice-PIN required (`actions/voice_pin.py`).
#### 36.10.6 Cameras / Sensors / Alarms (VA-IOT-005) — HA entities; motion events become Supervisor bus `info` messages.
#### 36.10.7 Appliances / Cleaners / Irrigation (VA-IOT-006) — via HA.
#### 36.10.8 Energy / Electricity (VA-IOT-007) — HA sensors; feeds KPI resolver `energy_kwh_today`.
#### 36.10.9 Scenes / Routines (VA-IOT-008) — via HA.
#### 36.10.10 Presence / Geofencing (VA-IOT-009) — HA person entities; Supervisor subscribes and fires routines.
#### 36.10.11 Room / Group Control (VA-IOT-010) — HA areas.
#### 36.10.12 Device Status / Diagnostics (VA-IOT-011) — HA state query.
#### 36.10.13 Local-only Mode (VA-IOT-012) — HA runs locally; privacy-max.
#### 36.10.14 Safety Interlocks (VA-IOT-013) — rules engine: `config/safety_interlocks.json` (`if door.open then oven.off` style). Enforced by Supervisor tick.

**Phase.** I (one phase delivers all IoT via the HA bridge).

---

### 36.11 Media & Entertainment

#### 36.11.1 Music Streaming — Spotify / Apple Music (VA-MED-001)
- **Approach.** Spotify Web API + Apple MusicKit JS (via local bridge). OAuth user-side flow.
- **Files.** `actions/music.py` (unified interface), `plugins/music_spotify.py`, `plugins/music_apple.py`. Secrets via existing `core/secrets.py`.
- **Tools.** `music.play`, `music.pause`, `music.skip`, `music.queue_add`, `music.search`, `music.create_playlist`, `music.add_to_playlist`.
- **Phase.** F.

#### 36.11.2 Podcasts / Radio (VA-MED-001)
- **Approach.** Podcast index API (`podcastindex.org`) + RSS feeds. TuneIn radio via unofficial URL scheme.
- **Files.** `actions/podcasts.py`, `memory/podcasts.sqlite` (subscriptions, play positions).
- **Phase.** F.

#### 36.11.3 Multi-room Audio (VA-MED-006)
- **Approach.** AirPlay 2 (`pyatv`), Chromecast (`pychromecast`), HA media_players.
- **Files.** `actions/multiroom.py`.
- **Phase.** I (ties to IoT).

#### 36.11.4 Playlists Management (VA-MED-003) — part of §36.11.1.

#### 36.11.5 Captions / Subtitles (VA-MED-004)
- **Approach.** For local video (VLC via `python-vlc`), toggle subtitle track. For streaming, open the player's captions overlay via GUI click (§36.8.3).
- **Phase.** F.

#### 36.11.6 Parental Controls (VA-MED-009)
- **Approach.** Content filter on music/video queries. Config: `config/parental_controls.json` with allowlist/denylist + per-child profiles (depends on §36.20 multi-user).
- **Phase.** J (after identity multi-user).

#### 36.11.7 Content Recommendations (VA-MED-010)
- **Approach.** Reuse user history + LLM: "You've been watching X; try Y." No dedicated recommender. On-request only.
- **Phase.** F.

---

### 36.12 Creative / Educational / Professional

#### 36.12.1 Image Editing by Voice (VA-EDU-006)
- **Approach.** `actions/image_edit.py` using Pillow + rembg + Real-ESRGAN (upscale) + simple ops (crop, rotate, filter, adjust). LLM parses NL → ops list.
- **Phase.** G.

#### 36.12.2 Audio Editing (VA-EDU-006)
- **Approach.** `actions/audio_edit.py` using `pydub` + `ffmpeg` (trim, normalize, fade, merge, convert, speed, pitch).
- **Phase.** G.

#### 36.12.3 Video Editing (VA-EDU-006)
- **Approach.** `actions/video_edit.py` using `moviepy` + `ffmpeg` (trim, concat, overlay audio, extract frames, add subtitles).
- **Phase.** G.

#### 36.12.4 Presentation Creation (VA-EDU-008)
- **Approach.** `actions/presentation.py` → emits `.pptx` via `python-pptx` and/or Google Slides API. LLM composes outline+content.
- **Phase.** H.

#### 36.12.5 Interactive Tutoring / Quizzing (VA-EDU-009)
- **Approach.** `actions/tutor.py` — quiz loop (question → user voice answer → evaluate → next). Persist in `memory/tutoring.sqlite` (`sessions`, `questions`, `answers`, `scores`).
- **Phase.** F.

#### 36.12.6 Language Practice (VA-EDU-010)
- **Approach.** Role `LanguageCoach` auto-synthesized; uses tutor loop (§36.12.5) with target-language TTS + user-speech correction.
- **Phase.** F.

#### 36.12.7 Data Analysis / Spreadsheets (VA-EDU-007)
- **Approach.** `actions/data_analyze.py` — accepts CSV/XLSX path, uses `pandas` via `python_exec` (sandbox). Produces chart via `matplotlib` → PNG.
- **Phase.** G.

#### 36.12.8 Design (VA-EDU-005)
- **Approach.** Depends on vision tooling; integrate Figma via existing `figma` skills if user authenticates. Minimal scope: NL → Figma spec via `figma-use` MCP tool.
- **Phase.** H.

---

### 36.13 Health, Wellbeing & Accessibility

**Safety overarching rule.** No diagnoses, no medication dosing, no treatment recommendations. All Health features include a persistent disclaimer and default-to-escalation on red flags. Explicit opt-in per feature.

#### 36.13.1 Medication Reminders (VA-HEA-002)
- **Approach.** `actions/medication.py` + `memory/medications.sqlite` (`medications(id, name, dose, schedule_cron, start_date, end_date, notes, active)`). Scheduler fires reminders. No dose calculation.
- **Phase.** F.

#### 36.13.2 Symptom Journaling (VA-HEA-004)
- **Approach.** `actions/symptoms.py` + `memory/symptoms.sqlite` (`symptoms(id, ts, symptom, severity_1_10, duration_min, notes, red_flag INT)`). LLM classifies red-flag (chest pain, stroke signs, etc.) → force escalation via `confirm.request("Call emergency contact?")`.
- **Phase.** F.

#### 36.13.3 Fall / Distress Detection (VA-HEA-007)
- **Approach.** Reuse §36.2.1 sound-event model (cry, scream, heavy thud). On high-confidence detection + no voice response within 20 s → initiate check-in call via Communicator role.
- **Phase.** G.

#### 36.13.4 Clinician Handoff / Emergency (VA-HEA-006)
- **Approach.** `actions/emergency.py` — dial emergency contact from profile; share location (requires location permission); read symptom journal. Platform-specific dialer bridge.
- **Phase.** F.

#### 36.13.5 Caregiver Workflows (VA-HEA-010)
- **Approach.** Role `Caregiver` + a shared workflow template `caregiver-daily-check-ins`. Multi-user mode (§36.20) required.
- **Phase.** J.

#### 36.13.6 Nutrition Tracking (VA-HEA-005)
- **Approach.** `actions/nutrition.py` — log food via voice → USDA FoodData Central API for macros → SQLite. Daily summary.
- **Phase.** F.

#### 36.13.7 Check-ins / Crisis Protocols (VA-HEA-011)
- **Approach.** Scheduled Supervisor nudges; on silence from user, escalate.
- **Phase.** F.

---

### 36.14 Commerce & Finance

#### 36.14.1 Product Discovery / Price Comparison (VA-COMM-001)
- **Approach.** `actions/shopping.py` — multi-site browser automation via existing `browser_control.py` + Amazon PA-API where user has credentials. Returns top 5 w/ price, rating, reviews summary.
- **Phase.** F.

#### 36.14.2 Order Status / Returns (VA-COMM-003)
- **Approach.** Browser automation against Amazon/eBay/Shopify order pages + email parse for shipping notifications.
- **Phase.** F.

#### 36.14.3 Banking Read-Only (VA-COMM-004)
- **Approach.** Plaid integration (`plugins/plaid.py`) with user-provided OAuth. Read balances + last transactions only. No write operations.
- **Files.** `plugins/plaid.py`, secrets via `core/secrets.py`.
- **Phase.** H.

#### 36.14.4 Fraud Alerts (VA-COMM-008)
- **Approach.** On new transaction via Plaid webhook: compare to user spending baseline (`core/finance.py` aggregates). Flag outliers → notify + optional LVL3 confirm "do you recognize this?"
- **Phase.** H.

#### 36.14.5 Elder-Fraud Regulatory Controls (VA-COMM-010)
- **Approach.** User-set daily spending cap + cooling-off period for new payees. Supervisor rejects cap-exceeding LVL3 payments before reaching `confirm.request`.
- **Phase.** L.

---

### 36.15 Travel, Mobility & Automotive

#### 36.15.1 GPS / Navigation (VA-TRA-001)
- **Approach.** `actions/navigation.py` — open Apple Maps / Google Maps / Waze with query. Live traffic via `google-maps-services-python` where API key present.
- **Phase.** F.

#### 36.15.2 Ride Booking (VA-TRA-002)
- **Approach.** Uber API or deep-link fallback. User OAuth once.
- **Phase.** G.

#### 36.15.3 In-Car Infotainment (VA-TRA-005)
- **Approach.** CarPlay/Android Auto are closed; we integrate via phone-side companion plus HA car integrations when available. Minimal direct control today.
- **Phase.** M (long-horizon).

#### 36.15.4 Vehicle Diagnostics (VA-TRA-006)
- **Approach.** OBD-II over Bluetooth (`python-OBD`). User plugs in dongle. `actions/vehicle.py` reads codes, battery, fuel.
- **Phase.** M.

#### 36.15.5 Driver Monitoring (VA-TRA-008)
- **Approach.** Research only in Phase M; voice-prosody drowsiness detector.

#### 36.15.6 EV Charging Finder (VA-TRA-007)
- **Approach.** Open Charge Map API via `actions/ev_charging.py`.
- **Phase.** G.

---

### 36.16 Enterprise / Contact Center

#### 36.16.1 ERP / HRIS Integration (VA-ENT-001)
- **Approach.** Build a generic MCP server `mcp_servers/erp_connector/` with pluggable backends (QuickBooks, Xero, Gusto). Each adapter exposes a narrow read+limited-write toolset.
- **Phase.** K (business-ops hardening).

#### 36.16.2 IVR / Call Routing (VA-ENT-004)
- **Approach.** Twilio Voice webhook + Programmable Voice. `actions/ivr.py` defines menus declaratively.
- **Phase.** K.

#### 36.16.3 Agent Assist (VA-ENT-005)
- **Approach.** During a live call (transcribed in real time), surface relevant KB snippets in dashboard. Reuses `grounded.py` + `research.py`.
- **Phase.** K.

#### 36.16.4 Call Transcription & Summarization (VA-ENT-006)
- **Approach.** Same pipeline as §36.2.2 but for phone audio. Twilio call recording → Gemini transcription → summary + action items → CRM log.
- **Phase.** K.

#### 36.16.5 Procurement Approval Workflows (VA-ENT-009)
- **Approach.** Workflow template `procurement-approval` + LVL3 gate per approval tier.
- **Phase.** K.

#### 36.16.6 Dispatch (VA-ENT-010)
- **Approach.** `actions/dispatch.py` — field-worker queue; mobile companion app out of scope. Minimal: manage queue + notify by SMS.
- **Phase.** K.

---

### 36.17 Multimodal & Sensor Fusion

#### 36.17.1 Gesture / Body Pose Recognition (VA-MMM-003)
- **Approach.** MediaPipe Pose on camera stream; Supervisor subscribes to pose events.
- **Phase.** F.

#### 36.17.2 Gaze Tracking (VA-MMM-004)
- **Approach.** MediaPipe Face Landmarker gaze estimation; used for "I'm looking at X, do Y" phrases.
- **Phase.** H.

#### 36.17.3 Object / Person Recognition (VA-MMM-007)
- **Approach.** Pre-trained YOLOv8n (small) for objects; FaceNet embeddings for enrolled people only (explicit opt-in).
- **Phase.** H.

#### 36.17.4 Scene Description for Accessibility (VA-MMM-008)
- **Approach.** On trigger, capture frame → Gemini Vision → narrate via TTS.
- **Phase.** F.

#### 36.17.5 Cross-Modal Grounding (VA-MMM-009)
- **Approach.** Shared context buffer already exists (`context_buffer.py`); extend with per-modality tagging so planner can refer to "the chart on screen" by index.
- **Phase.** G.

---

### 36.18 Proactive / Ambient / Agentic

Most of this is delivered by Phases A–E. Remaining:

#### 36.18.1 Rollback with Full Audit (VA-PRO-011)
- **Approach.** `core/undo.py` already exists (10-entry stack). Extend to a durable table `memory/undo_stack.sqlite` with (`id, workflow_id, label, undo_json (serialized call), executor, created_at, applied_at`). Every reversible LVL2 op pushes an undo. `actions/undo.py` lists + applies.
- **Phase.** F.

#### 36.18.2 Delegation to External Sub-Agents (VA-PRO-008)
- **Approach.** Role can specify `external_mcp_server` — Supervisor spawns the goal into a remote MCP server instead of a local sub-agent. The MCP server reports back via the standard agent bus (see §11).
- **Phase.** L.

---

### 36.20 Identity, Personalization & Memory

#### 36.20.1 Multi-user / Guest Mode (VA-ID-005)
- **Approach.** Add `user_id` column to `tasks`, `goals`, `workflows`, `vectors`, `journal` rows. New `users.sqlite` (`users(id, name, voiceprint_embedding, role, parental_restrict, created_at)`). Voice recognition uses existing audio pipeline + `pyannote` speaker embeddings — match incoming speaker to stored voiceprint. Guest mode = `user_id=0` + read-only scopes.
- **Phase.** J.

#### 36.20.2 Household vs Work Separation (VA-ID-006)
- **Approach.** Two orthogonal dimensions on every workflow: `user_id` + `context: 'home' | 'work' | 'personal'`. Dashboard filters by context.
- **Phase.** J.

#### 36.20.3 Child Safety / COPPA (VA-ID-009)
- **Approach.** User with `parental_restrict=1` gets a restricted role set + content filters (§36.11.6) + no LVL2 commerce + no outbound email/sms.
- **Phase.** J.

#### 36.20.4 Data Portability / Export (VA-ID-007)
- **Approach.** `actions/export_me.py` — produces encrypted ZIP of all user's rows across SQLite stores + vectors + memory + journal. One command.
- **Phase.** L.

---

### 36.21 Privacy, Security & Governance

#### 36.21.1 Hardware Mic Mute Integration (VA-SEC-006)
- **Approach.** macOS: observe Control Center mute state via `CoreAudio`. Windows: WASAPI mute state. Linux: PulseAudio/PipeWire mute. When muted, suspend all audio-reading actions + display banner.
- **Phase.** F.

#### 36.21.2 Anti-Spoof / Replay Detection (VA-SEC-009)
- **Approach.** For voice-authenticated actions: liveness challenge (random phrase readback) + anti-replay nonce. Reject if audio contains compressed/known artifacts.
- **Phase.** L.

#### 36.21.3 Prompt-Injection & Malicious Audio Defense (VA-SEC-010)
- **Approach.** Pre-filter all LLM inputs that originated from scraped web / email / document content through a sanitizer that strips imperative sentences targeting the model. Audio inputs normalized through ASR only — no raw-audio to LLM paths outside explicit multimodal tools.
- **Phase.** L.

#### 36.21.4 Data Retention Policy UI (VA-SEC-004)
- **Approach.** Dashboard screen listing every SQLite store with current retention setting + bulk-delete button. Backed by `core/retention.py` running a nightly prune pass.
- **Phase.** J.

#### 36.21.5 Formal Audit Logs (VA-SEC-013)
- **Approach.** `memory/audit.sqlite` — append-only, SQLite WAL + checksum per row (HMAC over previous row + current). Tamper-evident. Captures: every LVL3 confirm, every role synth, every workflow creation, every user_id attribution.
- **Phase.** L.

---

### 36.22 Reliability, Quality & Operations

#### 36.22.1 Hallucination Detection / Mitigation (VA-OPS-006)
- **Approach.** For factual LLM outputs, run a verifier pass (second LLM call with web-search tool) that asks "which of these claims are not supported by cited sources?" Low-confidence claims get removed or caveated. Reuses `core/grounded.py`.
- **Phase.** L.

#### 36.22.2 Adversarial Input Testing (VA-OPS-014)
- **Approach.** `tests/adversarial/` — red-team scenarios (prompt injection in email body, hidden-command in doc OCR, replay of past voice command). CI runs on every commit touching NLU or approvals.
- **Phase.** L.

#### 36.22.3 Full Localization / i18n (VA-OPS-013)
- **Approach.** `core/locale.py` (introduced in §36.1.4) grows to cover number/date/currency/timezone formatting + per-locale prompt templates in `prompts/locales/<ietf>/`. Dashboard i18n via `.po` files.
- **Phase.** M.

---

### 36.23 Deployment, Architecture & Hardware

#### 36.23.1 Containerization
- **Approach.** `Dockerfile` + `docker-compose.yml`. Base image with Python 3.12, system deps (ffmpeg, portaudio), volumes for `memory/` + `config/`. Audio I/O is host-passthrough; vision is optional (needs camera access).
- **Phase.** L.

#### 36.23.2 Autoscaling
- **Approach.** Not a single-user concern. Deferred as non-goal unless multi-tenant deployment is adopted.

#### 36.23.3 Formal SLA Monitoring
- **Approach.** Prometheus exporter on `/metrics` endpoint (dashboard). Grafana dashboards in `ops/grafana/`.
- **Phase.** L.

#### 36.23.4 Disaster Recovery
- **Approach.** Nightly SQLite backup (hot-copy via `.backup`) + optional rclone to user-specified cloud. `scripts/restore.py` validates integrity + replays.
- **Phase.** L.

---

### 36.24 Emerging & Future

#### 36.24.1 Real-Time Speech-to-Speech Translation (VA-FUT-001)
- **Approach.** Gemini Live now supports multilingual output; chain input language detect → Live session in target language. Latency expected 1.5–3 s end-to-end.
- **Phase.** M.

#### 36.24.2 Emotional Prosody Recognition / Feedback (VA-FUT-002)
- **Approach.** Pre-trained emotion classifier (e.g. `wav2vec2-emotion`) on 2 s audio windows → feed to Supervisor; Supervisor adjusts TTS tone via SSML prosody.
- **Phase.** M.

#### 36.24.3 Spatial / Embodied Avatars (VA-FUT-003)
- **Approach.** Existing `core/avatar.py` + `avatar_mesh.py` already render; extend with Three.js front-end in dashboard. Full embodiment deferred.
- **Phase.** N.

#### 36.24.4 Personalized Local Models (VA-FUT-004)
- **Approach.** LoRA fine-tune on user's past conversation + journal. Run on-device via `llama.cpp`. Switch personal-tone replies to local model; keep tool-use on Gemini for capability. Opt-in.
- **Phase.** N.

#### 36.24.5 Multi-Agent Collaboration (VA-FUT-005)
- **Approach.** Already partially covered by sub-agents + agent bus (Phase C). "Negotiation protocol" additions: agents can propose, counter-propose, vote. Supervisor adjudicates.
- **Phase.** N.

#### 36.24.6 Predictive Context (VA-FUT-007)
- **Approach.** Scheduled scan of upcoming calendar events + commitments; Supervisor proactively offers prep (driving directions, meeting notes, materials).
- **Phase.** F (quick win from existing data).

---

## 37. Extended Phased Delivery (Phases F–N)

Phases A–E in §31 deliver the orchestration layer. Phases F–N deliver the domain-gap features.

| Phase | Theme | Key features (§36 refs) | Dependencies |
|-------|-------|-------------------------|--------------|
| **F** | Communication + media + wellness quick wins | 36.1.2, 36.1.3, 36.1.4, 36.3.3, 36.7.1, 36.7.2, 36.7.4, 36.11.1, 36.11.2, 36.11.5, 36.11.7, 36.12.5, 36.12.6, 36.13.1, 36.13.2, 36.13.4, 36.13.6, 36.13.7, 36.14.1, 36.14.2, 36.15.1, 36.17.4, 36.18.1, 36.21.1, 36.24.6 | A–E |
| **G** | Audio deep + vision + creative tooling | 36.2.1, 36.2.2, 36.2.3, 36.8.1, 36.8.3, 36.12.1, 36.12.2, 36.12.3, 36.12.7, 36.13.3, 36.15.2, 36.15.6, 36.17.1, 36.17.5 | F |
| **H** | Spatial audio + voice cloning + banking + design | 36.3.1, 36.3.2, 36.12.4, 36.12.8, 36.14.3, 36.14.4, 36.17.2, 36.17.3 | G |
| **I** | Smart Home / IoT (via Home Assistant bridge) | 36.7.3, 36.10 (entire), 36.11.3 | F |
| **J** | Multi-user + child safety + retention UI | 36.11.6, 36.13.5, 36.20.1, 36.20.2, 36.20.3, 36.21.4 | F–I |
| **K** | Enterprise + call center | 36.16 (entire) | J |
| **L** | Security hardening + export + DR + containerization | 36.3.4, 36.8.2, 36.14.5, 36.18.2, 36.20.4, 36.21.2, 36.21.3, 36.21.5, 36.22.1, 36.22.2, 36.23.1, 36.23.3, 36.23.4 | I, J |
| **M** | Travel/automotive + full i18n + emerging A | 36.15.3, 36.15.4, 36.15.5, 36.22.3, 36.24.1, 36.24.2 | L |
| **N** | Research/speculative (opt-in experiments) | 36.24.3, 36.24.4, 36.24.5 | M |

Every phase is independently shippable. User may re-order or skip any phase after A–E.

---

## 38. Expanded Component Inventory for Domain-Gap Work

Approximate new file count across phases F–N:

| Phase | New Python modules | New DBs | New config JSONs | New assets |
|-------|--------------------|---------|-------------------|-------------|
| F | ~18 | 5 (voicemails, spam, medications, symptoms, nutrition, podcasts) | 3 | 0 |
| G | ~14 | 2 (meetings, tutoring) | 2 | 1 (YAMNet TFLite) |
| H | ~10 | 2 (voices, plaid_cache) | 2 | 1 (HRTF WAV) |
| I | ~6 | 1 (endpoints) | 2 | 0 |
| J | ~8 | 1 (users) | 1 | 0 |
| K | ~12 | 2 (calls, dispatch_queue) | 2 | 0 |
| L | ~14 | 1 (audit) | 2 | 0 |
| M | ~10 | 1 (vehicle_logs) | 1 | 0 |
| N | ~6 | 0 | 1 | 0 |

**Gross estimate across F–N: ~98 new Python files, ~15 new SQLite DBs, ~16 new JSON configs.** All auto-discovered by existing `ActionRegistry` / `PluginRegistry`. No existing module is replaced.

---

## 39. Hardware & Platform Dependencies

| Feature | Requires |
|---------|----------|
| Wake-gesture, pose, gaze, scene description | Webcam |
| Multi-party recording, diarization, fall detection (audio) | Good mic + optional array |
| Multi-room audio, IoT | Home Assistant instance on LAN (user installs) |
| Voice cloning | Opt-in enroll; needs 30 s clean sample |
| OBD vehicle diag | Bluetooth OBD-II dongle |
| Twilio IVR | Twilio account + phone number |
| Plaid banking | Plaid account + user OAuth |
| Spotify/Apple Music | User streaming subscription |
| Local LLM fine-tune | Mac with ≥16 GB RAM or Linux with GPU |
| Remote desktop | SSH-reachable hosts enrolled by user |

Every hardware-bound feature degrades gracefully: missing device → feature disabled with clear message, no crash.

---

## 40. Expanded Risk Register for Domain Gaps

| Risk | Phase | Likelihood | Impact | Mitigation |
|------|-------|------------|--------|------------|
| HA bridge token leak | I | Low | High | Store in `core/secrets.py` keyring; never log; rotate yearly |
| Voice clone misuse | H | Medium | High | Enroll-only on user's own sample w/ consent phrase; LVL3 per session |
| Fraud alert false positives spook user | H | Medium | Medium | Tunable thresholds; clear "this looks unusual — recognize it?" wording |
| Health feature mistaken for medical advice | F | Medium | High | Permanent disclaimer + red-flag escalation; never dosage math |
| Twilio phone costs surprise user | K | Medium | Medium | Daily spend cap in config; daily usage report |
| OBD read triggers car ECU warning | M | Low | Low | Read-only mode only; no active tests |
| Spam filter blocks a real sender | F | Medium | Medium | All auto-filtered items visible on dashboard w/ one-click un-filter |
| Multi-user voiceprint false-match | J | Medium | Medium | Low-confidence match prompts explicit voice PIN; never auto-applies elevated permissions |
| Audit log corruption | L | Low | High | HMAC chaining; integrity verifier run nightly; separate backup |
| Prompt-injection via scraped page | L | Medium | High | Pre-sanitize; no raw HTML into prompts; grounded citation; Phase L red-team suite |
| HA entity enumeration pulls thousands of tools → planner overload | I | Medium | Medium | Namespace filter; role-based tool filter (§8.4); only expose entities the user actively uses |
| Containerized Jarvis loses direct audio access | L | Medium | Medium | Document host-audio passthrough; non-container deployment stays primary |

---

---

## 41. Complete Feature-ID Matrix (every VA-XXX-NNN from PROJECT_OVERVIEW.md)

Exhaustive mapping. Every narrative ID is tabled with: current state, which phase delivers it, implementation reference.

Legend: ✅ DONE / 🟡 PARTIAL / 🔵 NEW / 🚫 OUT.

### 41.1–41.3 Activation / Audio / Speech Output

- **VA-UX 001–021 (Activation):** ✅ 7 (wake, PTT, turn-taking, timeouts, confirm, repair, session). 🟡 4 (ambient, personas, accessibility baseline, offline-local). 🔵 10 (gesture F, barge-in F, duplex F, IVR K, diarization J, whisper F, lang-switch F, prosody M, persona voice-map F, offline-extensions L).
- **VA-AUD 001–016:** ✅ 10. 🟡 2 (vocab B, lang-ID F). 🔵 4 (speaker recog J, diarization G, sound events G, meetings G).
- **VA-SPO 001–012:** ✅ 5. 🟡 3 (multi-voice F, multilingual F, phonetic F). 🔵 4 (SSML F, spatial H, cloning H, watermark L).

### 41.4–41.6 LU / Knowledge / Productivity

- **VA-NLU 001–015:** ✅ all 15.
- **VA-KNL 001–013:** ✅ all 13.
- **VA-PROD 001–013:** ✅ 9. 🟡 2 (goal tracking A, time-blocking D). 🔵 2 (presentations H, meetings G).

### 41.7–41.9 Comms / Desktop / Web

- **VA-COM 001–013:** ✅ 7. 🟡 2 (groups F, attachments F). 🔵 4 (voicemail F, video-conf F, intercom I, spam F).
- **VA-DEV (desktop) 001–012:** ✅ 7. 🟡 1 (troubleshooting G). 🔵 4 (GUI click G, peripherals G, updates/backups L, remote SSH L).
- **VA-WEB 001–011:** ✅ all 11.

### 41.10 Smart Home (VA-IOT 001–013)

All 13 🔵 NEW, Phase I, via Home Assistant MCP bridge (§36.10).

### 41.11–41.12 Media / Creative

- **VA-MED 001–010:** ✅ 0. 🟡 2 (audiobooks F/H, trivia F). 🔵 8 (streaming F, playlists F, captions F, recs F, multi-room I, soundscapes F, parental J, discovery F).
- **VA-EDU 001–011:** ✅ 4 (brainstorm, writing, translation, coding). 🟡 1 (coaching F). 🔵 6 (design H, image/audio/video edit G, data analysis G, presentations H, tutoring F, language F).

### 41.13 Health (VA-HEA 001–012)

✅ 3 (wellness, appointments, oversight). 🟡 1 (cognitive accessibility F). 🔵 8 (medication F, symptom F, nutrition F, clinician F, fall G, caregiver J, check-ins F, accessibility expand F).

### 41.14 Commerce (VA-COMM 001–010)

✅ 4 (budgets, bills, payments, education). 🟡 1 (cart ops F). 🔵 5 (product discovery F, returns F, banking H, fraud H, elder-fraud L).

### 41.15 Travel (VA-TRA 001–011)

✅ 2 (flights, itinerary). 🟡 2 (distraction limits M, rail/air/marine F). 🔵 7 (nav F, ride G, EV G, in-car M, OBD M, driver monitoring M, fleet K).

### 41.16 Enterprise (VA-ENT 001–011)

✅ 2 (KB, support). 🟡 4 (CRM/ERP/HRIS K, workforce K, sales K, audit L). 🔵 5 (IVR K, agent-assist K, transcripts K, procurement K, dispatch K).

### 41.17 Multimodal (VA-MMM 001–010)

✅ 3 (image OCR, screen, location). 🟡 2 (spatial I, temporal A-E). 🔵 5 (gesture F, gaze H, object H, scene F, cross-modal G).

### 41.18 Proactive (VA-PRO 001–013)

✅ 7. 🟡 3 (consented F, multi-step A-E, provenance 0+L). 🔵 3 (goal decomp A, delegation external L, rollback F).

### 41.19 Developer Ecosystem (VA-DEV2 001–012)

✅ all 12.

### 41.20 Identity (VA-ID 001–010)

✅ 3. 🟡 2 (memory controls J, consent J). 🔵 5 (multi-user J, home/work J, portability L, COPPA J, speaker-ID J).

### 41.21 Security (VA-SEC 001–018)

✅ 7. 🟡 7 (on-device L, consent J, mic/killswitch F, residency L, incident L, governance L, retention J). 🔵 4 (anti-spoof L, prompt-inject L, audit L, voice-auth watermark L).

### 41.22 Reliability (VA-OPS 001–017)

✅ 9. 🟡 5 (monitoring 0+L, eval L, accessibility L, support L, rollouts L). 🔵 3 (hallucination L, i18n M, adversarial L).

### 41.23 Deploy/Hardware (VA-ARCH 001–016)

✅ 9. 🟡 4 (cameras F, wearables M, policy B+L, identity J+L, resources L). 🔵 3 (telephony K, event-bus C, observability L). 🚫 1 (autoscaling).

### 41.24 Emerging (VA-FUT 001–010)

🔵 5 (S2S M, prosody M, embodied N, local models N, predictive F). 🟡 2 (multi-agent C+N, universal translation M). 🚫 3 (robot, ambient-wall, BCI).

### 41.25 Totals

- ✅ DONE: ~120
- 🟡 PARTIAL → complete across phases: ~55
- 🔵 NEW: ~115
- 🚫 OUT: 4 (robot, BCI, ambient-wall, autoscaling)

---

## 42. Coverage Summary

Every PROJECT_OVERVIEW.md feature ID that is neither already DONE nor explicitly OUT has a concrete delivery plan tied to a phase (A–N) and a §36 micro-plan (or existing module extension).

---

## 43. (Pre-review) Approval Request

Preserved for history — see §47 for the final consolidated approval request after architecture review.

---

## 44. Senior-Architect Review — Findings Beyond PROJECT_OVERVIEW.md

PROJECT_OVERVIEW.md is a strong *capability* taxonomy. It under-specifies the **engineering substrate** that makes a long-running, multi-agent, multi-domain assistant actually work in production over months of real use. This section lists every architectural gap the review surfaced, grouped into 18 categories, each mapped to a concrete mini-plan (§36.25+). No overengineering: only items whose absence would materially hurt reliability, cost, safety, or outcome at real-world load are added. Strikeable items flagged TIER 3.

### 44.1 Cost & Resource Economics

**Gap.** No per-workflow / per-role / per-user LLM spend tracking. A runaway sub-agent can burn the token budget silently. No cost-aware routing. No prompt-cache awareness.

**Add.**
- **§36.25 Cost Ledger.** `memory/costs.sqlite` rows per LLM call: `{id, ts, model, tier, prompt_tokens, completion_tokens, est_cost_usd, workflow_id, goal_id, role, agent_id}`. Wrapper around `core/gemini.call`. Dashboard rollups.
- **§36.26 Cost-Aware Model Router.** `core/model_router.py` — given `(task_kind, latency_budget, quality_floor)` picks model tier. Replaces hardcoded `tier=SMART` call sites gradually. Learns from telemetry.
- **§36.27 Prompt-Cache Strategy.** Normalize every LLM call to `[fixed_preamble, mostly_static_context, dynamic_user_block]` so Gemini context caching / Anthropic ephemeral cache hits >60 %. Measure hit rate.
- **§36.28 Budget Caps.** Per workflow + per role + per user. Env: `JARVIS_BUDGET_USD_DAILY`, `JARVIS_BUDGET_USD_PER_WORKFLOW`. Breach → Supervisor pauses offending workflow + surfaces to user.

**Phase 0.**

### 44.2 Reasoning Paradigms

**Gap.** Plan relies on a single Gemini SMART call per goal. No ReAct, no self-consistency, no CoVe, no debate for high-stakes.

**Add §36.29 Reasoning Modes.** `core/reasoning.py`: `reason(strategy, question, context) → ReasonResult`. Strategies: `single_pass`, `react`, `self_consistent_n`, `cove`, `debate_n`. Role spec gains `reasoning_strategy`. High-stakes roles (Reviewer, Finance-Analyst, Health-synth) default to `cove` or `self_consistent_3`. Cost-gated: only when `priority == critical` or `target_metric in {safety, health, finance}`.

**Phase D.**

### 44.3 Memory Architecture

**Gap.** Vectors + JSON only. Real assistants need episodic + semantic + procedural split, consolidation, decay, conflict resolution, lightweight KG.

**Add.**
- **§36.30 Memory Type Split.** `memory/memory.sqlite`: `episodes`, `facts (subject, predicate, object, confidence, source_ref, last_verified_at)`, `procedures` (recurring workflow patterns).
- **§36.31 Consolidation.** Nightly: dedup facts, decay low-confidence, promote recurring episodes to procedures.
- **§36.32 Conflict Resolution.** New fact contradicts existing → Supervisor raises `question` bus event or auto-resolves by source rank + recency.
- **§36.33 Light KG.** `facts` + `fact_edges(entity_a, entity_b, rel, weight)`. Recursive CTE traversal. No external graph DB.

**Phase B.**

### 44.4 Context Engineering

**Gap.** No LLM-context budgeting. Tool catalogue + role prompts + recent context can silently overflow.

**Add.**
- **§36.34 Context Budget Engine.** `core/context_budget.py` tracks token cost per injected section. Priorities `system > role_persona > goal > tool_catalogue > recent_context > long_term_recall`. Overflow → trim lowest first.
- **§36.35 Automatic Context Compression.** Long dialog arrays auto-summarized. Preserves tool-call evidence + user turns verbatim.
- **§36.36 Hierarchical Summaries.** Per workflow tiered summary (day → week → month).

**Phase 0.**

### 44.5 Tool Scalability

**Gap.** Planner receives *every* TOOL declaration. HA (I) will expose hundreds of entity tools. 500+ tools wreck planner quality + cost.

**Add.**
- **§36.37 Tool RAG.** Embed every TOOL `name + description` once. Per goal, retrieve top K (20–40) + mandatory baseline tools. Full-catalogue fallback only on low-confidence retrieval.
- **§36.38 Tool Capability Registry.** TOOL gains `capabilities: [search, write, read_file, send_message, …]`. Retrieval + role filter use it.
- **§36.39 Tool Namespacing.** Group related tools (`hvac.*`, `lights.*`, `calendar.*`); planner sees group summary + expand on demand.

**Phase B.** Critical before I.

### 44.6 Agent Lifecycle Governance

**Gap.** Synthesized roles persist forever. No versioning, rollback, or success tracking. Bad synth keeps being chosen.

**Add.**
- **§36.40 Role Versioning.** `roles.save_synthesized` writes w/ incrementing `version`. `roles.rollback(name, version)` restores.
- **§36.41 Role Scorecard.** Per role: workflows completed, success rate, avg user-feedback, avg goal variance. Below threshold → retired.
- **§36.42 Decision Log.** Every Supervisor decision (chose X, spawned agent, replanned, paused) written w/ explain string. Dashboard view.

**Phase B–C.**

### 44.7 Observability & Explainability

**Gap.** Flat journal. No cross-agent request tracing. No decision explainability. No continuous eval.

**Add.**
- **§36.43 OpenTelemetry Tracing.** Each user turn / schedule fire / webhook creates a trace. Spans: `supervisor.understand`, `supervisor.decompose`, `role.synthesize`, `agent.spawn`, `planner.plan_goal`, `worker.dispatch`, `tool.<name>`, `agent_bus.send`, `confirm.request`. Export to local Jaeger or OTLP.
- **§36.44 Session Replay.** Trace + journal + LLM I/O serialized → deterministic replay against stub.
- **§36.45 Explainability Hook.** One-line `why:` per non-trivial Supervisor decision.
- **§36.46 Continuous Eval.** `tests/golden_workflows/*` — recorded workflows w/ frozen LLM responses. CI re-runs on every Supervisor/planner/worker change.

**Phase 0** for §36.43+46; **A–E** for §36.45; **L** for §36.44.

### 44.8 Reliability Engineering

**Gap.** Repair + replan handle LLM failures. No external-API circuit breakers; no retry-with-backoff; no DLQ; no saga/compensations.

**Add.**
- **§36.47 Circuit Breaker.** `core/circuit.py` wraps each external integration. Opens after N failures, half-open probe, closes on success. Dashboard-visible state.
- **§36.48 Backoff + Jitter.** `@retry(attempts, base, jitter)` decorator standard on external wrappers.
- **§36.49 DLQ.** Agent-bus messages un-handled after N retries land in `agent_messages_dlq`.
- **§36.50 Saga.** Reversible LVL2 steps register a compensation. Multi-step failure at step K of N → Supervisor runs compensations K–1 … 1.

**Phase 0** for §36.47+48; **C** for §36.49; **F** for §36.50.

### 44.9 Security Hardening (beyond PROJECT_OVERVIEW)

**Gap.** PROJECT_OVERVIEW covers encryption, consent, step-up auth. Missing:

- **§36.51 Secret-Leak Scanner.** Every LLM output + every outbound email/message/post scanned for `*_KEY=…`, private-key headers, high-entropy base64, JWT. Redacts + raises `info` bus event.
- **§36.52 Capability Token Proxy.** `core/cap_tokens.py` exchanges long-lived OAuth for short-lived (15 min) scoped tokens. All external calls route through proxy.
- **§36.53 Plugin Signing.** `tools/sign_plugin.py` writes `plugin.sig` (Ed25519). Loader warns on unsigned.
- **§36.54 SQLCipher At-Rest.** Wrappers on `tasks`, `goals`, `vectors`, `audit`. Key from `core/secrets.py` → keyring.
- **§36.55 Output Safety Gate.** `core/safety_gate.py` scans LLM outputs for escalation-worthy content; routes to crisis resources (health) or blocks (CSAM/self-harm).

**Phase 0** for §36.51+55; **L** for §36.52–54.

### 44.10 Human-in-the-Loop Advanced

**Gap.** Approval is binary. No delegation (SMS/Slack), no batch, no diff preview, no preference learning.

**Add.**
- **§36.56 Approval Channels.** `confirm.request()` broadcasts to configured channels (dashboard, SMS via Twilio, Slack DM, iOS APNs push). Reply approves/rejects w/ signed token.
- **§36.57 Batch Approval.** "Approve all pending for workflow X" view w/ per-item preview.
- **§36.58 Preference Learning.** Track user approve/reject by `(tool, category, recipient_tier, amount_bucket)`. After N consistent → auto-approve w/ notification. Never for LVL3 financial > $100.
- **§36.59 Change Preview.** Before any LVL2 mutation, auto-generate a diff.

**Phase F.**

### 44.11 Learning Loop

**Gap.** Reflection outputs free-text lessons. No prompt tuning, no RLHF-lite dataset, no shadow A/B.

**Add.**
- **§36.60 Correction Capture.** User edits draft / cancels plan / overrides → `{context, agent_output, user_correction}` into `memory/corrections.sqlite`. Monthly abstracts rules.
- **§36.61 Prompt Versioning + A/B.** `prompts/registry/<name>/<version>.md`. Shadow-exec new version in parallel on non-critical workflows + compare outcomes.
- **§36.62 User Prefix Learning.** Rules compiled into a short user-specific prefix injected into Supervisor + role prompts.
- **§36.63 Rolling Eval.** Weekly replay last week's workflows against current prompts; detect regressions.

**Phase L.**

### 44.12 Data Engineering & Knowledge Management

**Gap.** Knowledge scattered across email/Slack/Notion/Docs/local. Jarvis sees only what it asks in the moment. Needs scheduled ingest + personal KG.

**Add.**
- **§36.64 Source Connectors + Scheduled Ingest.** `connectors/<source>.py` for Gmail, Outlook, Slack, Notion, Obsidian, Google Drive, GitHub/GitLab, Linear/Jira/Asana. Nightly incremental. Documents chunked semantically → `vectors` (namespace `source:<name>`).
- **§36.65 Entity Resolution.** `entities` table (`id, name, aliases[], emails[], phones[], handles[]`). Merge candidates in dashboard.
- **§36.66 Personal KG.** Builds on §36.33. Dashboard graph view.
- **§36.67 Spaced Repetition.** `actions/sr.py` — Anki-style schedule on flagged facts. Used by LanguageCoach + learning workflows.
- **§36.68 Hybrid Search.** `search(q)` combines BM25 (FTS5) + vector + lexical name-match; final rerank w/ `bge-reranker-base`. Default for all RAG paths.

**Phase K** for connectors; **G** for KG + SR; **Phase 0** for hybrid search.

### 44.13 Collaboration & Delegation

**Gap.** Single-user. Real workflows cross humans ("ask my spouse / vendor X").

**Add.**
- **§36.69 Human Delegate Protocol.** Role may `delegate_to_human: {contact_id, channel}`. Jarvis messages human w/ one-click status link. Reply updates goal.
- **§36.70 Shared Workflows.** `shared_with: [user_id]`. Second user acts.
- **§36.71 Vacation Mode.** User sets window; auto-ack inbound w/ user-authored OOO, hold non-critical, resume on return.

**Phase M.**

### 44.14 DX for Plugin Authors

**Gap.** Plugins exist; no SDK, docs, hot-reload, signing.

**Add.**
- **§36.72 Plugin SDK.** `docs/PLUGINS.md` + `scripts/new_plugin.py` scaffolder.
- **§36.73 Hot-Reload Dev.** `JARVIS_DEV_WATCH=1` — `watchdog` monitors `actions/` + `plugins/`; auto-reloads changed module.
- **§36.74 Permission Manifest.** TOOL dict gains `permissions: [network, filesystem.read/write, microphone, camera, …]`. Shown on first enable.

**Phase L.**

### 44.15 Testing Infrastructure

**Gap.** Unit + integration exist. Missing: deterministic LLM stub, golden replay, chaos, load.

**Add.**
- **§36.75 Deterministic LLM Stub.** `tests/fixtures/llm_stub.py` — record-replay. Zero actual LLM cost in tests.
- **§36.76 Golden Workflow Replay.** `tests/golden/<scenario>.jsonl` — frozen input + expected tree + expected terminal state.
- **§36.77 Chaos Harness.** `tests/chaos/` — injects tool failures, sub-agent crashes, SQLite contention, LLM timeouts.
- **§36.78 Load Simulator.** `tests/load/sim_users.py` — N users × M workflows; measures tick time + SQLite contention.

**Phase 0** for §36.75; **L** for §36.76–78.

### 44.16 Business / OKR Layer

**Gap.** Goals are flat targets. Need OKRs + SLA + cross-workflow priority arbitration + ROI.

**Add.**
- **§36.79 OKR Framework.** Workflow optionally modeled as Objective + 3–5 KRs. Superset of current goal model.
- **§36.80 SLA Tracker.** Workflow/role/sub-goal may declare SLAs (`respond_within_minutes`, `complete_by_date`). Breach triggers alerts.
- **§36.81 Priority Arbiter.** When demand > capacity, rank by `(priority, deadline, user-boost)`; low-priority defers; user sees why.
- **§36.82 ROI / Time-Saved.** Per workflow `time_saved_minutes` (vs user-reported manual baseline) + `revenue_influenced_usd`. Monthly ROI report.

**Phase D.**

### 44.17 Deployment Modes

**Gap.** Plan assumes one laptop. Users want headless home-server, mobile, cross-device sync.

**Add.**
- **§36.83 Headless Mode.** `python -m jarvis.headless` — no voice stack; scheduler + Supervisor + dashboard.
- **§36.84 Mobile Companion.** iOS/Android thin client (voice + dashboard + approvals). Logic stays home-server via local or Tailscale.
- **§36.85 Multi-Device Sync.** CRDT (Automerge) replication of core tables between user's own devices. Opt-in.
- **§36.86 Cloud Backup.** Encrypted replication of SQLite stores to user-specified S3/rclone.

**Phase L** for §36.83+86; **M** for §36.84+85.

### 44.18 Compliance & Governance

**Gap.** Need DSAR, right-to-be-forgotten, data classification, policy engine.

**Add.**
- **§36.87 DSAR / Export Flow.** `actions/dsar.py` — encrypted ZIP + human-readable PDF index.
- **§36.88 Right-to-Be-Forgotten.** `actions/forget_me.py --scope` — cascading delete. Preserves audit log entries (immutable) with PII redacted.
- **§36.89 Data Classification.** Sensitive rows gain `classification` (`public`/`internal`/`pii`/`phi`/`pci`/`confidential`). Reports composer honors classification + channel policy.
- **§36.90 Policy Engine.** `core/policy.py` — declarative YAML rules ("`phi` never leaves device", "`pci` never in SMS", "`confidential` never logged for observability"). Evaluated on every outbound action.

**Phase L.**

---

## 45. Prioritization Tiering

### TIER 1 — essential foundational (Phase 0, prerequisite to A)

| Item | Why |
|------|-----|
| §36.25 Cost ledger | safety vs runaway spend |
| §36.26 Cost-aware router | big token-cost reduction |
| §36.27 Prompt-cache strategy | ditto |
| §36.28 Budget caps | safety |
| §36.34 Context budget engine | avoid silent truncation |
| §36.35 Auto context compression | enable long workflows |
| §36.36 Hierarchical summaries | same |
| §36.37 Tool RAG | mandatory before Phase I |
| §36.43 OpenTelemetry | impossible to diagnose later without |
| §36.47 Circuit breakers | essential for external integrations |
| §36.48 Backoff + jitter | same |
| §36.51 Secret-leak scanner | safety non-negotiable |
| §36.55 Output safety gate | same |
| §36.68 Hybrid search | big retrieval win across all RAG paths |
| §36.75 Deterministic LLM stub | makes every test cheap |
| §36.46 Continuous eval harness | catches regressions from day 1 |

### TIER 2 — high value, in natural phases

| Item | Phase |
|------|-------|
| §36.29 Reasoning modes | D |
| §36.30–33 Memory split + KG | B |
| §36.38–39 Tool capability + namespacing | B |
| §36.40–42 Role versioning + scorecard + decision log | B–C |
| §36.44 Session replay | L |
| §36.45 Explainability | A–E |
| §36.49 DLQ | C |
| §36.50 Saga | F |
| §36.56–59 Approval channels + batch + preference + preview | F |
| §36.60–63 Corrections + A/B + user prefix + rolling eval | L |
| §36.64–67 Connectors + entity res + KG + SR | G/K |
| §36.69–71 Human delegate + shared + vacation | M |
| §36.79–82 OKR + SLA + priority + ROI | D |

### TIER 3 — strikeable (user decides)

| Item | Rec. phase |
|------|-----------|
| §36.52 Capability tokens | L |
| §36.53 Plugin signing | L |
| §36.54 SQLCipher | L |
| §36.72–74 Plugin SDK + hot-reload + manifest | L |
| §36.77 Chaos harness | L |
| §36.78 Load sim | L |
| §36.83 Headless | L |
| §36.84 Mobile | M |
| §36.85 CRDT sync | M |
| §36.86 Cloud backup | L |
| §36.87–90 DSAR + RTBF + classification + policy | L |

### STRIKE

- Agent marketplace — social; single user does not need. Drop.
- Robot manipulation, BCI, ambient-wall, autoscaling — stay 🚫 OUT.

---

## 46. Revised Phase Schedule

New **Phase 0 (Hygiene)** inserted as prerequisite to A.

| Phase | Theme | Review additions |
|-------|-------|------------------|
| **0 — Hygiene** | Cost + context + retrieval + reliability + safety + eval foundation | §36.25, 26, 27, 28, 34, 35, 36, 37, 43, 46, 47, 48, 51, 55, 68, 75 |
| **A — Foundation** | (unchanged) + §36.45 |
| **B — Roles + Templates** | + §36.30–33, 38–39, 40–42 |
| **C — Sub-agents + Bus** | + §36.49 |
| **D — Eval + Reporting** | + §36.29, 79–82 |
| **E — Polish** | unchanged |
| **F — Comms + media + wellness** | + §36.50, 56–59 |
| **G — Audio + vision + creative** | + §36.66–67 |
| **H — Spatial + voice clone + banking + design** | unchanged |
| **I — Smart Home** | unchanged (RAG-tools from 0 required) |
| **J — Multi-user** | unchanged |
| **K — Enterprise + ingest** | + §36.64–65 |
| **L — Security + ops + export** | + §36.44, 52–54, 60–63, 72–74, 76–78, 83, 86, 87–90 |
| **M — Travel/auto + i18n + emerging-A + mobile** | + §36.69–71, 84–85 |
| **N — Research/speculative** | unchanged |

---

## 47. Final Consolidated Priority List & Approval Request

**Scope:**

1. **Phase 0 (NEW).** 16 foundational micro-plans (TIER 1).
2. **Phases A–E.** Orchestration layer augmented w/ TIER 2 items (formal memory + KG, tool registry, role versioning, scorecard, decision log, DLQ, OKR, SLA, priority arbiter, ROI, reasoning modes).
3. **Phases F–N.** All PROJECT_OVERVIEW.md gaps + architecture-review TIER 2 extensions (approval channels, saga, KG, SR, connectors, entity resolution, replay, A/B prompts, user prefix learning, rolling eval, plugin SDK, signing, manifest, golden replay, chaos, load, SQLCipher, headless, cloud backup, mobile, multi-device, DSAR, RTBF, classification, policy engine, human delegate, shared workflows, vacation).

**Totals:**

- ✅ DONE: ~120
- 🟡 → ✅: ~55
- 🔵 NEW: ~135 + ~15 architecture modules
- 🚫 OUT: 5 (robot, BCI, ambient-wall, autoscaling, agent marketplace)

**Awaiting user approval on:**

1. **Scope.** Strike any TIER 3 items now if desired.
2. **Phase order.** `0 → A → B → C → D → E → F → G → H → I → J → K → L → M → N`. Reorder if desired.
3. **Strike list.** Confirm current OUT list; add any TIER 3 to strike.
4. **Cost cap default.** Recommend `JARVIS_BUDGET_USD_DAILY=2`. OK?
5. **Phase 0 go-ahead.** ~2 weeks implementation effort; must land before A.

**No code until user signs off on this consolidated plan.** On approval, implementation starts **Phase 0 only**; subsequent phases re-approved one at a time.

---

## 48. Open Items for User Decision

- (i) Approve Phase 0 as prerequisite.
- (ii) Reasoning modes (§36.29): all four (ReAct + CoVe + self-consistency + debate) or subset?
- (iii) Ingest connectors (§36.64) priority order? (Default: Gmail > Slack > Notion > Google Drive > Linear > GitHub.)
- (iv) Approval channels (§36.56): dashboard always on; which of SMS / Slack / APNs to ship first?
- (v) SQLCipher at-rest (§36.54): default-on or opt-in?
- (vi) Cloud backup (§36.86) target: S3, Backblaze B2, rclone-any? User-provided?
- (vii) Mobile companion (§36.84): native app (6–12 wk) or thin web-view (2 wk)?
- (viii) Policy engine (§36.90): hand-rolled DSL or Open Policy Agent (Rego)?
- (ix) OTel exporter (§36.43): local Jaeger, OTLP endpoint, or both?
- (x) Strike list — anything else?

Plan final after answers. Then coding begins.

---

## 49. Deep Code Audit — Sub-Feature Completion Matrix

Deep read of every module in `actions/`, `core/`, `main.py`, `dashboard/`, `market/`, `memory/`, `tests/` revealed concrete sub-feature gaps *inside modules already tagged ✅ DONE* at the domain level. These are small but numerous. Each item below is a one-line concrete method addition to an existing action module — low-complexity, high-value.

### 49.1 Email (`actions/email_manager.py`)

| Missing sub-feature | Add | Phase |
|---------------------|-----|-------|
| `email.move_to_folder` | AppleScript Move command / IMAP MOVE | F |
| `email.archive` | mark + move to Archive | F |
| `email.delete` | LVL3 w/ trash fallback | F |
| `email.mark_read` / `mark_unread` | AppleScript flags set | F |
| `email.mark_important` / `star` | flag toggle | F |
| `email.reply(inline)` / `reply_all` | compose w/ quoted body | F |
| `email.forward` | compose w/ attach + body | F |
| `email.search(query, from, date, folder)` | server-side search | F |
| `email.get_attachments` + `save_attachment_to(path)` | enumerate + save | F |
| `email.create_folder` / `rename_folder` | mailbox mgmt | F |
| `email.get_thread` | conversation grouping | F |
| `email.snooze(until_ts)` | hide + reappear | F |

### 49.2 Calendar (`actions/calendar_manager.py`)

| Missing sub-feature | Add | Phase |
|---------------------|-----|-------|
| `calendar.update_event` | AppleScript set props | F |
| `calendar.delete_event` | LVL2 delete | F |
| `calendar.create_recurring` | RRULE builder | F |
| `calendar.add_attendees` + send invite | invitees block | F |
| `calendar.respond_invite(accept/decline/tentative)` | inbox action | F |
| `calendar.set_reminder(minutes_before)` | alarm attach | F |
| `calendar.find_free_slot(duration, window, attendees)` | availability search across attendees via web | F |
| `calendar.list_by_calendar(name)` | multi-calendar filter | F |
| `calendar.change_calendar(event_id, calendar)` | move event | F |
| `calendar.search(query, date_range)` | title + notes search | F |
| `calendar.set_location(event_id)` | location field | F |
| `calendar.set_videoconf(zoom/meet/teams)` | auto-link gen | F |

### 49.3 Contacts (`actions/contacts_manager.py`)

Currently read-only (`search` only). Add write operations:

| Missing sub-feature | Add | Phase |
|---------------------|-----|-------|
| `contacts.create(name, emails, phones, org, notes)` | AppleScript add | F |
| `contacts.update(id, field, value)` | AppleScript set | F |
| `contacts.delete(id)` | LVL3 | F |
| `contacts.merge(id_a, id_b)` | dedupe | F |
| `contacts.add_to_group(id, group)` + `remove_from_group` | group mgmt | F |
| `contacts.export_vcard(id | all)` | vCard writer | F |
| `contacts.import_vcard(path)` | bulk import | F |
| `contacts.bulk_update(filter, change)` | sweeping edits | F |
| `contacts.last_contact_date(id)` | query CRM interactions | F |
| `contacts.birthday_calendar_sync` | push CRM birthdays to Calendar | F |

### 49.4 Reminders / Todo (`actions/reminder.py`, `actions/todo.py`)

| Missing sub-feature | Add | Phase |
|---------------------|-----|-------|
| `reminder.complete(id)` / `uncomplete` | status flag | F |
| `reminder.update(id, field)` | edit | F |
| `reminder.delete(id)` | LVL2 | F |
| `reminder.list_by_list(name)` | multi-list filter | F |
| `todo.subtask_create(parent_id, text)` | hierarchical | F |
| `todo.add_tag(id, tag)` / `remove_tag` | tag management | F |
| `todo.recurring(cron_expr)` | repeat rule | F |
| `todo.share(user_id)` | post-multi-user J | J |
| `todo.import_from(source=apple_reminders|google_tasks|todoist)` | bidirectional sync | K |

### 49.5 Browser (`actions/browser_control.py`, `browser_flow.py`)

| Missing sub-feature | Add | Phase |
|---------------------|-----|-------|
| `browser.get_cookies(domain)` / `set_cookie` / `delete_cookie` | cookie jar ops | F |
| `browser.get_attributes(selector, attrs)` | DOM attr read | F |
| `browser.wait_for_function(js, timeout)` | custom JS wait | F |
| `browser.get_computed_style(selector, prop)` | layout query | F |
| `browser.intercept_requests(pattern, action)` | mock / log / block | H |
| `browser.set_geolocation(lat, lng)` | spoof for site testing | H |
| `browser.print_to_pdf(path)` | page → PDF | F |
| `browser.devtools_toggle()` | open inspector | H |
| `browser.focus_element(selector)` / `blur` | focus mgmt | F |
| `browser.screenshot_region(bbox)` | partial screenshot | F |
| `browser.save_session` / `restore_session` | cookie + storage snapshot | J |
| `browser.identity_profile(name)` | multi-persona browsing | J |
| `browser_flow.conditional_branch(condition, if_true, if_false)` | flow logic | F |
| `browser_flow.loop(steps, max)` | retry patterns | F |
| `browser_flow.extract(steps_of_data)` | scraping orchestration | F |

### 49.6 File Controller (`actions/file_controller.py`)

| Missing sub-feature | Add | Phase |
|---------------------|-----|-------|
| `file.chmod(path, mode)` | permission set | F |
| `file.symlink(src, dst)` | link create | F |
| `file.bulk_move(pattern, dest)` | glob-based bulk | F |
| `file.bulk_rename(pattern, replace)` | regex rename | F |
| `file.compress(paths, archive_path, format)` | zip/tar/7z | F |
| `file.decompress(archive_path, dest)` | extract | F |
| `file.encrypt(path, password)` / `decrypt` | symmetric encryption | L |
| `file.hash(path, algo)` | integrity check | F |
| `file.diff(a, b)` | textual diff | F |
| `file.watch(path, event, handler)` | already exists; extend w/ size/hash filter | F |

### 49.7 Code / Dev (`actions/code_helper.py`, `dev_agent.py`)

| Missing sub-feature | Add | Phase |
|---------------------|-----|-------|
| `code.refactor(selection, intent)` | targeted refactor | F |
| `code.add_tests(file)` | generate pytest/jest | F |
| `code.static_analyze(path)` | ruff/pylint/mypy/eslint | F |
| `code.profile(script)` | cProfile / line_profiler | F |
| `code.deps_audit(path)` | `pip-audit` / `npm audit` | F |
| `git.commit(message)` / `git.push` / `git.pull` / `git.status` / `git.diff` | git CLI wrapper | F |
| `git.create_pr(title, body, base, head)` | `gh pr create` | F |
| `git.review_pr(id)` | fetch + LLM review | F |
| `git.issue_create(title, body)` / `issue_close` | `gh issue` | F |
| `docker.build(path, tag)` / `docker.run` / `docker.push` | Docker CLI | G |
| `ci.trigger_build(repo, branch)` | `gh workflow run` | G |
| `release.notes(from_tag, to_tag)` | git log → LLM notes | F |

### 49.8 Web Search (`actions/web_search.py`)

| Missing sub-feature | Add | Phase |
|---------------------|-----|-------|
| `web.search_images(query)` | SerpAPI / DDG images | F |
| `web.search_videos(query)` | DDG videos / YouTube | F |
| `web.search_scholar(query)` | Semantic Scholar / arXiv | G |
| `web.search_patents(query)` | USPTO / Google Patents | K |
| `web.search_legal(query, jurisdiction)` | CourtListener | L |
| `web.search_gov_data(query, source)` | data.gov / census / Fed | G |
| `web.search_news(query, source, date_range, lang)` | newsapi / GDELT | F |
| `web.search_social(query, platform)` | X / Reddit / HN | F |

### 49.9 CRM (`actions/crm.py` / `core/crm.py`)

| Missing sub-feature | Add | Phase |
|---------------------|-----|-------|
| `crm.bulk_import(csv_path)` | mass add | F |
| `crm.export(format)` | CSV / vCard dump | F |
| `crm.merge(id_a, id_b)` | dedup | F |
| `crm.campaign_create(name, contacts, template)` | outreach series | K |
| `crm.campaign_run(id)` | throttled send | K |
| `crm.last_contact_days(id)` | staleness | F |
| `crm.suggest_reach_out()` | tier × staleness | F |
| `crm.enrich(id, source)` | pull public data | K |
| `crm.timeline(id)` | chronological view | F |

### 49.10 Finance (`actions/finance.py`, `core/finance.py`)

| Missing sub-feature | Add | Phase |
|---------------------|-----|-------|
| `finance.receipt_ocr(image_path)` | OCR → structured expense | G |
| `finance.categorize_auto` | ML/LLM categorization | F |
| `finance.tax_report(year)` | aggregate by category + export CSV | F |
| `finance.net_worth_snapshot` | sum assets − liabilities | H |
| `finance.budget_forecast(months_ahead)` | linear projection | F |
| `finance.subscription_audit` | unused / duplicate detect | F |
| `finance.invoice_send(id, recipient)` | PDF + email | F |
| `finance.recurring_detect(months)` | subscription miner | F |

### 49.11 Background Monitor (`actions/background_monitor.py`)

| Missing sub-feature | Add | Phase |
|---------------------|-----|-------|
| `monitor.update(id, field)` | edit monitor | F |
| `monitor.set_source_filter(id, sources)` | source allowlist | F |
| `monitor.set_frequency(id, cron)` | per-monitor cadence | F |
| `monitor.hash_body` | dedup on content, not just title | F |
| `monitor.set_language(id, lang)` | non-English | F |

### 49.12 Ai-Site (`actions/ai_site.py`)

| Missing sub-feature | Add | Phase |
|---------------------|-----|-------|
| `ai_site.switch_model(site, model)` | Claude 4.6 vs 4.7, GPT-4 vs 5 | F |
| `ai_site.delete_chat(site, id)` | housekeeping | F |
| `ai_site.list_chats(site)` | history | F |
| `ai_site.rename_chat(site, id, title)` | organize | F |
| `ai_site.attach_image(site, path)` | per-site file upload | F |
| `ai_site.share_chat(site, id)` | public link | F |

### 49.13 Core Runtime Half-built Items (from `main.py` + `core/`)

| Item | Current | Fix | Phase |
|------|---------|-----|-------|
| Session resumption handle capture | configured but never captured from server | wire in handle capture → `session_store.save_handle` | A |
| Affective dialog | dropped with model switch | restore on gemini native-audio model when available | M |
| Vision stream scene-change filter | "cheap" filter commented as TODO | implement frame-diff gating to cut LLM cost | G |
| Session summary save | 1–2 sentence only | hierarchical summary (§36.36) | 0 |
| Multi-session per process | mono-session | future: multiple parallel sessions per sub-agent voice channel (deferred to Gemini Live multi-session support) | N |
| Emotion detector | offline pattern-match | upgrade to wav2vec emotion (already planned §36.24.2) | M |

### 49.14 Market Subsystem Gaps

| Missing | Plan phase |
|---------|-----------|
| Multi-exchange support (Alpaca, IB, Kraken, Bybit, dYdX) | H |
| Live execution bridge (CCXT) | H (opt-in, LVL3 per order) |
| Stocks + forex + commodities data (Alpaca / Polygon / Finnhub) | H |
| Options + futures data (read-only) | H |
| News sentiment feed (replace stub) — MarketAux / Benzinga / Finnhub | K |
| Macro data (FRED, BLS) | G |
| On-chain data (Dune, Glassnode) | K |
| Portfolio correlation + sector rotation | H |
| Walk-forward validation | L |
| Additional strategies (mean reversion, breakout, pairs, ML) | H |
| Research UI in dashboard | H |

### 49.15 Memory Subsystem Gaps

| Missing | Plan phase |
|---------|-----------|
| Schema-version table + migration framework | 0 |
| Journal auto-prune / rotate | 0 |
| Encrypted-at-rest for all stores (§36.54 SQLCipher) | L |
| Backup + restore CLI (`memory_backup.py`) | L |
| Memory reconciliation across devices (CRDT merge) | M |

### 49.16 Dashboard Gaps (beyond routes already in plan)

| Missing | Plan phase |
|---------|-----------|
| Workflow DAG / tree visualization w/ live progress | A |
| Role editor (persona + tool filter + autonomy sliders) | B |
| Agent inbox view | C |
| Evaluations chart (score over time per goal) | D |
| Report viewer + PDF download | D |
| Secret / credential manager UI | L |
| MCP server status + logs | L |
| Config diff / history view | L |
| Knowledge graph viewer (D3) | G |
| Voice session replay player | L |
| Mobile-optimized layout | M |

---

## 50. Universal AI-Assistant Capability Checklist

Beyond PROJECT_OVERVIEW.md, every "complete AI assistant" needs the capability categories below. Many overlap with the orchestration + domain plans already in place; this section enumerates what remains and tiers each item.

### 50.1 Natural Conversation Refinements

| Capability | State | Phase |
|-----------|-------|-------|
| Repair strategies (re-ask, rephrase, offer alternatives) | 🟡 PARTIAL | A–E (planner replan path exists) |
| Small-talk / chit-chat mode | ✅ | — (Gemini baseline) |
| Context-aware formality calibration | 🔵 NEW — add preference slider | F |
| Humor / wit calibration | 🔵 NEW — add preference slider | F |
| Empathy response modes | 🟡 PARTIAL (emotion.py) | M (§36.24.2) |
| Graceful interrupt-resume | 🟡 PARTIAL | F (§36.1.3) |
| User-tone mirroring | 🔵 NEW | F |
| Clarifying question discipline | ✅ | — (confirm.py + planner) |

### 50.2 Core AI Competencies

| Capability | State | Phase |
|-----------|-------|-------|
| Chain-of-thought exposure (opt-in) | 🔵 NEW — add `verbose_reasoning` toggle | D |
| Confidence calibration | 🔵 NEW — all factual responses carry 0–1 score | L |
| "I don't know" discipline | 🟡 PARTIAL — refusal policy exists; extend w/ explicit "unknown" tag | A |
| Source attribution granularity | 🟡 PARTIAL — `grounded.py` cites | A |
| Opinion vs fact distinction | 🔵 NEW | F |
| Counter-perspective / steel-man mode | 🔵 NEW | F |
| Fact-check mode toggle | 🟡 PARTIAL — `grounded.py` | F |
| Bias / loaded-language detection | 🔵 NEW | L |

### 50.3 Agent Self-Awareness

| Capability | State | Phase |
|-----------|-------|-------|
| "What are you doing right now?" | 🔵 NEW — list active workflows + agents + inflight tools | A |
| "What tools do you have?" | 🔵 NEW — enumerate registered tools | A |
| Self-diagnostic ("something is slow — why?") | 🔵 NEW — check resource_gate + circuit breakers + queue depth | 0 |
| Model / version awareness | 🔵 NEW — "I'm running on Gemini 3 and Opus 4.7" | A |
| Capability advertisement | 🔵 NEW — describe own role + autonomy + reach | A |
| Limitation acknowledgement | ✅ | — (honest-communication protocol) |

### 50.4 Learning From the User

| Capability | State | Phase |
|-----------|-------|-------|
| Correction capture | 🔵 NEW | L (§36.60) |
| Explicit teaching ("remember that X means Y") | ✅ | — (`save_memory` tool exists) |
| Preference extraction from behavior | 🔵 NEW | L (§36.58, §36.62) |
| Routine detection ("you always X on Mondays") | 🔵 NEW | L — via procedural-memory mining (§36.31) |
| Error-pattern learning | 🔵 NEW | L (§36.60) |

### 50.5 Context Sharing / Universal Input

| Capability | State | Phase |
|-----------|-------|-------|
| Screen share → analyze | ✅ (vision_analyze + screen_processor) | — |
| File drag-drop → appropriate handler | 🔵 NEW — universal-input dispatcher | F |
| Clipboard-read for context | 🟡 PARTIAL | F |
| Voice-memo transcription | 🔵 NEW | F |
| Photo of document → OCR + act | 🟡 PARTIAL (vision_analyze) | F |
| Any-URL → unfurl + ingest | 🔵 NEW | F |

### 50.6 Accessibility Beyond Baseline

| Capability | State | Phase |
|-----------|-------|-------|
| Dyslexia-friendly text formatting | 🔵 NEW | F |
| Font size / contrast control | 🔵 NEW (dashboard pref) | F |
| Audio description for images (opt-in) | 🔵 NEW | F |
| Sign language output | 🚫 OUT (hardware/model) | — |
| Keyboard-only full operation | 🟡 PARTIAL | F |

### 50.7 User-Facing Settings

| Capability | State | Phase |
|-----------|-------|-------|
| Personality slider | 🔵 NEW | F |
| Verbosity slider | 🟡 PARTIAL | F |
| Formality slider | 🔵 NEW | F |
| Interruption tolerance | 🔵 NEW | F |
| Confirmation strictness | 🟡 PARTIAL — per role autonomy | F |
| Memory retention preferences | 🔵 NEW | J (§36.21.4) |
| Preferred language / locale | 🔵 NEW | M (§36.22.3) |
| Voice selection | 🟡 PARTIAL | F |

### 50.8 Communication Smoothing

| Capability | State | Phase |
|-----------|-------|-------|
| Email tone per recipient tier | 🔵 NEW | F |
| Formal ↔ casual code-switch | 🔵 NEW | F |
| Multi-lingual user (English Q → Spanish answer) | 🔵 NEW | M |
| Inbox triage rules per contact | 🟡 PARTIAL (gatekeeper) | F |

### 50.9 Content Pipelines

| Capability | State | Phase |
|-----------|-------|-------|
| Newsletter authoring | 🔵 NEW | F (Writer role + Substack API) |
| Blog publish (Medium, Substack, Ghost) | 🔵 NEW | F |
| Podcast scripting + episode planning | 🔵 NEW | G |
| Video script → shot list → B-roll search | 🔵 NEW | G |
| Social post scheduler (X, LinkedIn, IG) | 🔵 NEW | F |
| Instagram image + caption bundle | 🔵 NEW | G |
| YouTube chapter/title/description SEO | 🔵 NEW | G |
| Thumbnail generation | 🔵 NEW | H (image-gen) |

### 50.10 Research Deeper

| Capability | State | Phase |
|-----------|-------|-------|
| Academic (Semantic Scholar / arXiv / PubMed) | 🔵 NEW | G |
| Patent search | 🔵 NEW | K |
| Legal case search | 🔵 NEW | L |
| News archive deep-cut (GDELT) | 🔵 NEW | F |
| Gov data (data.gov / census / FRED / BLS / Eurostat) | 🔵 NEW | G |
| Stocks / commodities / forex live | 🔵 NEW | H |
| Scientific paper summarize + follow-up refs | 🔵 NEW | G |

### 50.11 Analytics for User Life

| Capability | State | Phase |
|-----------|-------|-------|
| Life dashboard (time on topics, energy patterns) | 🔵 NEW | D |
| Sleep + activity integration (Health, Fitbit, Oura) | 🔵 NEW | G |
| Financial health score | 🔵 NEW | H |
| Relationship health (last contact per CRM tier) | 🟡 PARTIAL | F |
| Workflow success-rate chart | 🔵 NEW | D |
| Mood pattern analysis (from symptom journal) | 🔵 NEW | F |
| Productivity trendline | 🟡 PARTIAL (productivity_report) | D |

### 50.12 Automation Ops

| Capability | State | Phase |
|-----------|-------|-------|
| Scheduled reports via email | 🔵 NEW | D |
| Regular check-in calls / messages with contacts | 🔵 NEW — `Negotiator` role + scheduler | F |
| Automatic birthday handling (card + gift suggest) | 🟡 PARTIAL (birthdays.py) | F |
| Routine task completion on behalf of user | ✅ — Supervisor loop | A–E |
| Automatic follow-up detection + nudge | 🟡 PARTIAL (commitments.py) | F |
| Deadline-slip early warning | 🔵 NEW — `at_risk` already modeled | A |

### 50.13 Browser Deep

| Capability | State | Phase |
|-----------|-------|-------|
| Session save/restore (cookie + storage) | 🔵 NEW | J |
| Multiple identity profiles | 🔵 NEW | J |
| Bookmark organization | 🔵 NEW | F |
| Password-manager integration (Bitwarden / 1Password) | 🔵 NEW | L |
| Extension install/remove via API | 🔵 NEW | G |
| Browser-sync (user's own) | 🔵 NEW | M |

### 50.14 Document Pipelines

| Capability | State | Phase |
|-----------|-------|-------|
| PDF form fill | 🔵 NEW | G |
| DocuSign integration | 🔵 NEW | K |
| Contract redlining | 🔵 NEW | G |
| Contract template library | 🔵 NEW | K |
| Legal / HR letter generation | 🔵 NEW | F |
| Resume tailoring per job | 🔵 NEW | F |
| Pitch-deck builder | 🔵 NEW | H |
| Document OCR → structured extract | 🟡 PARTIAL (vision_analyze) | G |

### 50.15 Creative Pipelines

| Capability | State | Phase |
|-----------|-------|-------|
| Image generation (SDXL local / Imagen / DALL·E 3) | 🔵 NEW | H |
| Music generation (Suno / Udio) | 🔵 NEW | H |
| 3D asset generation | 🔵 NEW | N |
| Logo / brand kit generation | 🔵 NEW | H |
| Video generation (Runway / Sora via API) | 🔵 NEW | N |

### 50.16 Developer Deep

| Capability | State | Phase |
|-----------|-------|-------|
| Git commit / push / PR / review | 🔵 NEW | F (§49.7) |
| Docker build + run + push | 🔵 NEW | G |
| Package mgmt (pip / npm / cargo / brew) | 🔵 NEW | F |
| Test runner (pytest / jest / cargo test) | 🔵 NEW | F |
| Linter / formatter runner | 🔵 NEW | F |
| CI/CD trigger (GH Actions / GitLab CI) | 🔵 NEW | G |
| Dependency vulnerability scan | 🔵 NEW | G |
| Code review automation | 🔵 NEW | F |
| Issue triage | 🔵 NEW | F |
| Release-notes generation | 🔵 NEW | F |
| Monorepo awareness (nx / turbo / bazel) | 🔵 NEW | L |

### 50.17 Operations

| Capability | State | Phase |
|-----------|-------|-------|
| SSH to host, check services | 🔵 NEW | L (§36.8.2) |
| Log analysis / grep / tail | 🔵 NEW | L |
| Deploy rollback trigger | 🔵 NEW | L |
| Alert routing (PagerDuty / Opsgenie) | 🔵 NEW | L |
| Status-page updates | 🔵 NEW | L |
| Server health dashboard | 🔵 NEW | L |

### 50.18 Education Deep

| Capability | State | Phase |
|-----------|-------|-------|
| Personalized curriculum generation | 🔵 NEW | F |
| Textbook summarization | 🔵 NEW | G |
| Flashcard set generation | 🔵 NEW | G (§36.67) |
| Exam prep mode | 🔵 NEW | F |
| Writing coach | 🔵 NEW | F |
| Math solver w/ step explanation | 🔵 NEW | F |
| Essay grading (practice only) | 🔵 NEW | F |

### 50.19 Social Operations

| Capability | State | Phase |
|-----------|-------|-------|
| Group event coordination (Doodle-like) | 🔵 NEW | F |
| Gift tracking per person | 🔵 NEW | F |
| Anniversary reminders | 🟡 PARTIAL (birthdays) | F |
| Event RSVP tracking | 🔵 NEW | F |
| Multi-participant scheduling | 🔵 NEW | K |

### 50.20 Travel Deep

| Capability | State | Phase |
|-----------|-------|-------|
| Visa requirements lookup | 🔵 NEW | F |
| Currency exchange tracking | 🟡 PARTIAL (timezone_util) | F |
| Travel-insurance comparison | 🔵 NEW | G |
| Expense report from trip | 🔵 NEW | F |
| Carbon footprint | 🔵 NEW | F |
| Loyalty program mgmt | 🔵 NEW | G |
| Lounge-access check | 🔵 NEW | G |
| Baggage tracker | 🔵 NEW | G |

### 50.21 Health Deep

| Capability | State | Phase |
|-----------|-------|-------|
| Doctor appointment booking | 🔵 NEW | F |
| Prescription refill tracking | 🔵 NEW | F (§36.13.1) |
| Insurance claim help | 🔵 NEW | L |
| Mental health check-in / mood log | 🔵 NEW | F |
| Hydration / steps / sleep goals | 🔵 NEW | F |
| Symptom pattern analysis | 🔵 NEW | F |
| Vaccination record | 🔵 NEW | F |
| Wearable data ingest (Apple Health / Fitbit / Oura / Garmin) | 🔵 NEW | G |

### 50.22 Home Operations

| Capability | State | Phase |
|-----------|-------|-------|
| Grocery list auto-build from meal plan | 🔵 NEW | F |
| Meal planning | 🔵 NEW | F |
| Recipe scaling / unit convert | 🔵 NEW | F |
| Shopping list divided by store | 🔵 NEW | F |
| Service scheduling (plumber, cleaner) | 🔵 NEW | F |
| Home inventory (pantry tracking) | 🔵 NEW | F |
| Chore rotation (multi-user, J) | 🔵 NEW | J |

### 50.23 Professional

| Capability | State | Phase |
|-----------|-------|-------|
| Resume tailoring | 🔵 NEW | F |
| LinkedIn post scheduler + optimizer | 🔵 NEW | F |
| Networking outreach drafts | 🔵 NEW | F |
| Pitch-deck builder | 🔵 NEW | H |
| Investor update email | 🔵 NEW | F |
| OKR tracking | 🔵 NEW | D (§36.79) |
| Performance-review self-writing | 🔵 NEW | F |
| Interview prep / mock (voice loop) | 🔵 NEW | F |

### 50.24 Legal / Admin

| Capability | State | Phase |
|-----------|-------|-------|
| Document signing workflow | 🔵 NEW | K (§50.14) |
| Tax prep helper (not filing) | 🔵 NEW | F (§49.10) |
| Immigration paperwork tracker | 🔵 NEW | K |
| Government form helper | 🔵 NEW | K |
| Insurance comparison | 🔵 NEW | L |
| Will / estate document drafting (template only, w/ disclaimer) | 🔵 NEW | L |

### 50.25 Emergency / Safety

| Capability | State | Phase |
|-----------|-------|-------|
| Panic button → text contacts | 🔵 NEW | F (§36.13.4 extend) |
| Location share | 🔵 NEW | F |
| Emergency services dial | 🔵 NEW | F (§36.13.4) |
| Silent-alarm mode | 🔵 NEW | F |
| Trusted-contact escalation | 🔵 NEW | F |

### 50.26 Cross-Device Continuity

| Capability | State | Phase |
|-----------|-------|-------|
| Start on laptop, continue on phone | 🔵 NEW | M (§36.84, §36.85) |
| Context handoff | 🔵 NEW | M |
| Device-aware output format | 🔵 NEW | M |
| Device-specific voice profile | 🔵 NEW | M |

### 50.27 Backup / Data Ops

| Capability | State | Phase |
|-----------|-------|-------|
| Auto-backup of important files | 🔵 NEW | L (§36.23.4, §36.86) |
| Photo library organize | 🔵 NEW | G |
| Duplicate photo detection | 🟡 PARTIAL (file_dedupe) | G |
| Old-file archival | 🔵 NEW | L |
| Cloud-drive sync status | 🔵 NEW | L |

### 50.28 Entertainment Companion

| Capability | State | Phase |
|-----------|-------|-------|
| Show tracking (next episode) | 🔵 NEW | F |
| Movie recommendations w/ reasons | 🟡 PARTIAL | F (§36.11.7) |
| Book club mode | 🔵 NEW | F |
| Concert / event discovery | 🔵 NEW | F |
| Reading list + queue | 🔵 NEW | F |

### 50.29 Writing Assistant Deep

| Capability | State | Phase |
|-----------|-------|-------|
| Style consistency across docs | 🔵 NEW | F |
| Plagiarism check | 🔵 NEW | G |
| Citation formatter (APA / MLA / Chicago / Harvard / IEEE / Vancouver) | 🔵 NEW | F |
| Thesaurus / rhyme / rhetorical devices | 🔵 NEW | F |
| Outline → draft expansion | ✅ | — (LLM) |
| Draft → polish iteration tracking | 🔵 NEW | F |
| Book-length ghostwriting workflow | 🔵 NEW | N |

### 50.30 Pricing / Commerce

| Capability | State | Phase |
|-----------|-------|-------|
| Price tracker on specific items | 🔵 NEW | F |
| Deal alerts | 🔵 NEW | F |
| Coupon auto-apply | 🔵 NEW | F |
| Price-history graph | 🔵 NEW | F |
| Camelcamelcamel / Keepa API | 🔵 NEW | F |

### 50.31 Agents Chaining With External

| Capability | State | Phase |
|-----------|-------|-------|
| Call other AI services as tools (Zapier-style) | 🟡 PARTIAL (MCP) | — |
| Workflow export to n8n / Zapier / Make | 🔵 NEW | L |
| External webhook as trigger | ✅ | — (`core/webhooks.py`) |

### 50.32 AI Meta Operations

| Capability | State | Phase |
|-----------|-------|-------|
| Compare model outputs side-by-side | 🔵 NEW | L |
| Benchmark on user's tasks | 🔵 NEW | L |
| Switch model on the fly | 🟡 PARTIAL (ai_adapters) | F |
| Temperature / sampling override | 🔵 NEW | L |
| Cost-per-response display | 🔵 NEW | 0 (§36.25) |

### 50.33 Observation

| Capability | State | Phase |
|-----------|-------|-------|
| Screen recording for later analysis | 🔵 NEW | G |
| Private voice-log journal | 🔵 NEW | G |
| Searchable conversation history (full-text + semantic) | 🟡 PARTIAL | 0 (§36.68) |

### 50.34 Well-being / Posture

| Capability | State | Phase |
|-----------|-------|-------|
| Screen-time awareness | 🔵 NEW | F |
| Break reminders | 🔵 NEW | F |
| Posture check reminders | 🔵 NEW | F |
| Eye-strain alerts (20-20-20) | 🔵 NEW | F |
| Pomodoro integration | 🔵 NEW | F |

### 50.35 Universal Export / Integration Hooks

| Capability | State | Phase |
|-----------|-------|-------|
| Export any dataset to CSV / JSON / Parquet | 🔵 NEW | F |
| Universal "send to" (anywhere Jarvis can send) | 🔵 NEW | F |
| Hotkey → run saved workflow | 🔵 NEW | F |
| Share workflow result as link / QR | 🔵 NEW | F |

---

## 51. Grand Total & Final Approval Request

**Scope (cumulative Phases 0 + A–N):**

- **PROJECT_OVERVIEW.md IDs:** ~300 (§41 maps each).
- **Architecture-review additions:** 66 items (§36.25–90).
- **Sub-feature gaps inside existing actions:** ~90 methods added (§49).
- **Universal AI-assistant capabilities checklist:** ~190 capabilities across 35 categories (§50).

**Cumulative scoreboard:**

| Bucket | Count |
|--------|-------|
| ✅ DONE | ~130 |
| 🟡 PARTIAL → ✅ | ~80 |
| 🔵 NEW | ~500 |
| 🚫 OUT | 7 (robot, BCI, ambient-wall, autoscaling, marketplace, sign-language output, video-gen Sora — deferred) |

**Expected gross effort:**

| Phase | Est. weeks | Comment |
|-------|-----------|---------|
| 0 | 2 | Foundational hygiene (TIER 1) |
| A | 2 | Supervisor + goal hierarchy |
| B | 2 | Roles + templates + memory split + KG + tool registry |
| C | 2 | Sub-agents + agent bus + DLQ |
| D | 2 | Evaluation + reporting + OKR + SLA + priority arbiter + reasoning modes |
| E | 1 | Polish |
| F | 3 | Comms + media + wellness + content pipelines + extended sub-features (§49 + §50) |
| G | 3 | Audio deep + vision + creative edit + KG + SR + health deep |
| H | 3 | Spatial audio + voice cloning + banking + markets + design + content-creation pipelines |
| I | 2 | Smart Home (HA bridge) |
| J | 2 | Multi-user + child safety + retention UI |
| K | 3 | Enterprise + ingest connectors + legal/admin |
| L | 3 | Security hardening + container + ops + exports + classification + policy engine |
| M | 3 | Travel/auto + full i18n + mobile + multi-device + emerging-A |
| N | 2 | Research/speculative + book-length workflows + 3D/video gen |

**Total: ~35 weeks** of focused implementation to achieve full universal-assistant capability (modulo TIER 3 strikes).

**Phase 0 is strictly prerequisite; nothing else starts until it ships.**

### Final approval asks:

1. **Confirm scope.** All five layers in scope (PROJECT_OVERVIEW IDs + arch review + action sub-features + universal checklist + strike list)?
2. **Confirm phase order.** `0 → A → B → C → D → E → F → G → H → I → J → K → L → M → N`?
3. **Strike list.** Currently 🚫 OUT: robot, BCI, ambient-wall, autoscaling, agent marketplace, sign-language output, external video-gen (Sora). Add any more?
4. **TIER 3 decisions** (strikeable now if desired): capability-token proxy, plugin signing, SQLCipher default-on, plugin SDK, chaos harness, load sim, headless mode, mobile native vs web-view, CRDT sync, cloud backup, full DSAR + RTBF + classification + policy engine.
5. **Cost cap default.** `JARVIS_BUDGET_USD_DAILY=2` OK?
6. **Phase 0 go-ahead.** ~2 weeks; must land before A.

**Open items from §48 still pending user answers.**

---

## 52. Coding Start Contract

Explicit user sign-off on the above ⇒ implementation begins.

Order of first week (Phase 0 only):

1. `core/migrations.py` + idempotent ALTERs on `tasks`, `vectors`.
2. `core/cost_ledger.py` + wrap `gemini.call`.
3. `core/model_router.py` stub (wraps existing tier choices for now; learns later).
4. `core/context_budget.py` + priority-based trimming.
5. `core/circuit.py` + `@retry` decorator.
6. `core/tool_rag.py` + embed every TOOL once.
7. `core/safety_gate.py` + `core/secret_scanner.py`.
8. `core/observability.py` OTel spans scaffolding.
9. `tests/fixtures/llm_stub.py` deterministic replay.
10. `tests/golden/` first workflow freeze.
11. CI check for all above.

Each PR is small, reviewed, merged, verified. No step advances before the prior lands green.

---

## 53. Final-Sweep Addendum — Capabilities Still Missing

Honest check. Previous §§41–52 cover most dimensions, but a disciplined re-pass surfaced more. Added below so plan truly reaches 100 % coverage before coding.

### 53.1 Interaction Modalities (beyond voice + dashboard + mobile)

| Capability | Phase |
|-----------|-------|
| Haptic feedback (watch/phone vibration patterns) | M |
| AR overlay (Vision Pro, Meta Glasses) | N |
| Sketch input (draw to describe) | H |
| Whiteboard mode (freehand + reasoning overlay) | H |
| Co-pilot mode (continuous observation + suggestions) | G |
| Pair-programming mode | F |
| Driver / eyes-free mode (voice-only, simplified) | F |
| Lock-screen widget | M |
| Watch complication | M |
| Menu-bar quick-ask | F |
| Spotlight-style quick invoke | F |

### 53.2 AI Output Quality

| Capability | Phase |
|-----------|-------|
| Confidence intervals on numeric predictions | L |
| Error bars / probability on timelines | L |
| Alternative explanations ("another way to see this") | L |
| Falsifiability check ("what would change my mind") | L |
| Multi-answer modes (brief + detailed + example) | F |
| Self-critique before finalizing | D (§36.29 CoVe already covers) |
| Citation density per claim | A |

### 53.3 Advanced Agent Ops

| Capability | Phase |
|-----------|-------|
| Role hot-swap mid-workflow | C |
| Forked workflows (A/B trial) | L |
| Workflow merging | L |
| Workflow diff (compare runs) | L |
| Dry-run / shadow mode per step (default on destructive) | F |
| Sandbox-first by default for shell/code | F |
| Human as first-class role in agent bus | M (§36.69 extended) |
| Agent registry + search (every synthesized role ever) | B |
| Agent retirement curve | B |
| Promote synthesized role → built-in | E |
| Agent reputation score | C |

### 53.4 Trust & Verifiability

| Capability | Phase |
|-----------|-------|
| Signed outputs (Ed25519 sig per artifact) | L |
| Reproducible plans (seed-based) | D |
| Audit replay tool (CLI) | L |
| Full explain-chain tree (one tap on any decision) | L |
| Provenance tracker per memory fact | B (§36.30 already) |

### 53.5 Personalization Depth

| Capability | Phase |
|-----------|-------|
| Themes (dashboard + voice persona skin) | F |
| Custom wake word ("Hey Nova", "Computer") | F |
| Custom voice name (user-assigned) | F |
| Custom hotkeys per workflow | F |
| Personalized briefing time + content mix | F |
| Preferred units (metric/imperial, 12/24h) | F |
| Preferred date format | F |
| Preferred currency primary + conversions | F |
| Cultural calendar overlays (lunar, Islamic, Hebrew, etc.) | M |
| Dietary / religious constraint awareness | F |

### 53.6 Content Safety Extras

| Capability | Phase |
|-----------|-------|
| IP respect (refuse large-chunk copyrighted output) | L |
| Trademark awareness in generated content | L |
| Age-gate for mature topics | J (§36.11.6 extend) |
| Jailbreak resistance evaluation suite | L |
| Impersonation refusal (no pretending to be a real person) | L |
| Deepfake refusal (no voice/face swap) | L |

### 53.7 Emotional Intelligence Beyond Prosody

| Capability | Phase |
|-----------|-------|
| Celebrate wins (milestone hit → acknowledgment) | F |
| Acknowledge losses (goal missed → empathy, not blame) | F |
| Burnout pattern detection (cross-signal) | G |
| Gentle nudging (frequency decay on repeated refusals) | F |
| Boundary respect (user "stop" → immediate halt + remember) | F |
| Celebration variety (not scripted) | F |
| Grief / bereavement mode | M |
| Life-event detection (new job, baby, move, loss) → re-plan | M |

### 53.8 Longitudinal Care

| Capability | Phase |
|-----------|-------|
| Yearly review (auto-generated) | D |
| Monthly check-in call | D |
| Goals-missed cumulative analysis | D |
| Habits-stuck analysis | D |
| "Where you were a year ago" retrospective | D |
| Memory time-capsule (write now, Jarvis delivers in 5 yr) | F |
| Holiday calendar awareness per culture | M |
| Jubilee / anniversary tracking | F |

### 53.9 Autonomy Boundaries

| Capability | Phase |
|-----------|-------|
| Explicit "do nothing" mode (listen-only) | F |
| Scheduled quiet hours per day | F |
| Per-item "never do X without asking" overrides | F |
| Reversibility ladder shown before every action (low/med/high) | F |
| One-click revoke autonomy across all roles | A |
| Session kill-switch via phrase ("Jarvis freeze") | A |

### 53.10 Knowledge Freshness

| Capability | Phase |
|-----------|-------|
| Model-knowledge-cutoff awareness in outputs | A |
| Fact-age tracking in memory ("you learned this 6 months ago") | B |
| Automatic fact-refresh schedule | B |
| "Is this still true?" background verifier | D |
| News impact on stored facts (trigger update) | K |

### 53.11 Error UX

| Capability | Phase |
|-----------|-------|
| User-friendly error narration (no stack traces to user) | 0 |
| "Here's what happened + what we can try" template | 0 |
| Escalate-vs-retry decision tree | 0 |
| Error-trend dashboard (most common failures) | L |

### 53.12 Privacy UX

| Capability | Phase |
|-----------|-------|
| "What do you know about me?" one-command | J |
| Delete topic from memory (one command) | J |
| PII scrub before any outbound message | L (§36.51 extended) |
| Private mode (session not stored) | F |
| Incognito voice session | F |
| Export-and-delete workflow | L (§36.87+88) |

### 53.13 Platform Integrations Not Yet Listed

| Platform | Phase | Notes |
|----------|-------|-------|
| Apple Shortcuts | F | bidirectional run + define |
| iMessage | F | existing bridge extend |
| FaceTime | F | start call |
| AirPods / BT HID controls | F | click-to-mute, focus |
| HomeKit | I | via HA bridge |
| Microsoft Graph (Outlook / Teams / OneDrive) | K | OAuth |
| Google Workspace (full admin where allowed) | K | OAuth |
| iCloud (Notes / Drive / Reminders / Calendar) | K | CloudKit / API |
| OneNote | K | Graph API |
| Todoist / TickTick / Things | K | API each |
| Twitter/X API | F | post / search / DM (rate-limited) |
| Reddit | F | read + post |
| Mastodon / Bluesky | F | fediverse post + read |
| Instagram (Graph API, business account) | F | post + insights |
| TikTok (business API) | F | post + insights |
| WhatsApp Business API | F | message send |
| Telegram bot | F | message send |
| Signal (via signal-cli) | F | message send |
| Discord bot | F | message + slash cmd |
| Teams bot | F | message + card |
| Zoom SDK | F | schedule + join |
| Webex | K | schedule + join |
| Loom | G | upload + transcript |
| Canva | H | template fill |
| ClickUp / Monday / Airtable | K | project mgmt |
| Supabase / Firebase | L | dev ops |
| Square / Freshbooks / Zendesk / Intercom | K | business ops |
| Sentry / Datadog / Splunk | L | observability platforms |
| Lastpass / Dashlane | L | password mgr |
| Mint / YNAB / Personal Capital | H | finance read |
| Yelp / TripAdvisor / Booking / Airbnb / Expedia / Kayak / Hopper | G | travel |
| Google Flights / Rome2Rio / Citymapper / Transit / Moovit | G | travel |

### 53.14 Life-Area Coverage Not Yet Carved

| Area | Phase |
|------|-------|
| Pet care (feeding, vet, exercise, meds) | F |
| Garden / plant care (watering, fertilizer schedule) | F |
| Hobby tracking (practice logs) | F |
| Collection mgmt (books, music, games, wine) | F |
| Car maintenance (oil, tyres, registration) | M |
| Home maintenance log + warranty tracker | F |
| Mental fitness (puzzles, brain games) | F |
| Spiritual practice (meditation timer, prayer times) | F |
| Volunteer tracking | F |
| Charity giving tracker | F |
| Political engagement (voting, reps, bills) | F |
| Family-tree + genealogy integration | N |
| Grief / bereavement rituals | M |

### 53.15 Accessibility Deep (neurodivergent + specific needs)

| Capability | Phase |
|-----------|-------|
| ADHD mode (chunking, nudges, visual timers) | F |
| Autism-support mode (predictability, change warnings) | F |
| Depression check-in (opt-in, non-therapeutic) | F |
| Anxiety support (breathing, grounding exercises) | F |
| OCD-respectful patterns (no counting triggers unprompted) | F |
| Dyslexia text formatting | F |
| High-contrast / colorblind palettes | F |
| Deaf-friendly outputs (captions-first, visual cues) | F |
| Motor-impairment UI (long-press tolerance, no-dexterity ops) | F |

### 53.16 Universal File Support

| Format | Phase |
|--------|-------|
| RAW photos (DNG, CR2, NEF, ARW) | G |
| CAD (DWG / DXF / STEP / IGES — via conversion) | G |
| 3D (STL / OBJ / GLTF / FBX / USDZ) | G |
| DAW projects metadata (Logic, Ableton, Pro Tools) | G |
| NLE projects metadata (Final Cut, Premiere, DaVinci) | G |
| DOCX with track changes | F |
| LaTeX | G |
| Markdown w/ math (KaTeX / MathJax) | F |
| Encrypted containers (Veracrypt, age) | L |
| eBooks (EPUB, MOBI, PDF) | F |
| Spreadsheets (XLSX, ODS, Numbers) | F |
| Presentations (PPTX, Keynote, ODP) | F |

### 53.17 Journal / Self-Questioning / Memoir Modes

| Capability | Phase |
|-----------|-------|
| Journal mode (daily prompt + entry) | F |
| Gratitude prompt | F |
| Socratic self-questioning | F |
| Long-form interview mode (user narrates → structured) | G |
| Autobiography / memoir drafting workflow | N |
| Life-timeline builder | N |

### 53.18 Dry-Run / Safety on Destructive Ops

| Capability | Phase |
|-----------|-------|
| Preview before every irreversible operation | F |
| Undo countdown ("sending in 5s — cancel?") | F |
| Two-step for high-value payments (>= threshold) | L |
| "Rehearsal" workflow mode (nothing committed) | L |
| Shadow mode for high-stakes role actions | L |

### 53.19 Observation Deeper

| Capability | Phase |
|-----------|-------|
| Posture detection via camera | G |
| Hydration tied to activity | G |
| Biometric baseline tracking | G |
| Keystroke-/mouse-pattern anomaly (focus loss detect) | G |
| Ambient-noise classification (focus-friendly or not) | G |

### 53.20 Meta: What's Still Explicitly OUT

Confirmed stay out (user may override):

- Robotic manipulation (VA-FUT-006)
- BCI (VA-FUT-010)
- Ambient-wall computing (VA-FUT-008)
- Autoscaling (VA-ARCH-016)
- Agent marketplace (social ecosystem)
- Sign-language output
- Third-party video gen (Sora) direct integration — deferred Phase N optional
- Live financial-market trading real money (paper only; user may opt-in Phase H w/ strict LVL3)
- Medical diagnostic authority (always escalate; disclaimers; never prescribe)
- Legal counsel authority (templates + refs only; no advice)

### 53.21 Reality-Check Confirmations

What is already in the plan + confirmed not missed:

- Voice stack (STT, wake word, hotkey, TTS, audio I/O, echo)
- Vision stack (screen OCR, camera analyze, scene narrate — F)
- Multi-LLM backend (Gemini + Ollama + REST ladder + model router)
- Memory split (episodic / semantic / procedural + KG + vectors + long_term)
- Approval LVL1/2/3 + role autonomy + confirm-channels
- Killswitch + resource gate + circuit breakers + DLQ + saga
- Planner + sub-planner / replanner / repair loop + sub-agents + agent bus
- Scheduler (cron / interval / once / evaluation / report meta)
- Reflection + continuous eval + golden replay + chaos + load sim
- Evaluation + reports + OKR + SLA + priority arbiter + ROI
- Workflow lifecycle (persistable, pause, resume, cancel, close, retro, template-save)
- Roles (seed + synthesized + versioned + scorecarded + retired + promoted)
- Templates (seed + saveable + searchable + applicable)
- Approval channels (dashboard + SMS + Slack + APNs)
- Multi-user + guest + child-safety + voiceprint + home-vs-work
- Security (secret-scanner, cap-tokens, SQLCipher, output safety gate, policy engine, audit log, SBOM-ish via signing)
- Compliance (DSAR, RTBF, classification)
- Deployment (headless + mobile + CRDT + cloud backup + container)
- Dev-ex (plugin SDK + hot-reload + signing + permission manifest)
- Observability (OTel + session replay + explain-chain + dashboards)
- Cost (ledger, model router, prompt cache, budget caps)
- Context engineering (budget engine, auto-compression, hierarchical summaries)
- Tool scalability (RAG for tools, capability registry, namespacing)
- All 24 PROJECT_OVERVIEW.md domains with per-ID mapping
- All ~90 action sub-feature gaps
- All ~190 universal-AI-assistant checklist items

### 53.22 Final Confidence Statement

Plan now enumerates every category a "complete AI assistant" a senior architect would check for, across:

1. **Reasoning & NLU** (§4, §36.29)
2. **Memory & knowledge** (§36.30–33, §36.64–68, §50.1, §53.10)
3. **Tool use** (§36.37–39, §49, §50)
4. **Agency & orchestration** (§§5–20, §36.40–42, §44.6, §53.3)
5. **Voice / multimodal / accessibility** (§41.1–3, §50.6, §53.1, §53.15)
6. **Domain coverage** (§41, §50)
7. **Reliability** (§44.8, §36.47–50, §53.11)
8. **Security + privacy + compliance** (§44.9, §44.18, §53.6, §53.12)
9. **Observability + explainability** (§44.7, §53.4)
10. **Learning / self-improvement** (§44.11, §50.4)
11. **Cost + resource** (§44.1, §53.11)
12. **Deployment + continuity** (§44.17, §50.26, §53.1)
13. **Human factors** (§44.10, §50.1, §50.7, §53.5, §53.7, §53.9)
14. **Longitudinal care + life events** (§53.7, §53.8)
15. **Integrations breadth** (§36.10, §36.16, §50, §53.13)
16. **Testing + eval** (§44.15, §44.7)
17. **Business / OKR / analytics** (§44.16, §50.11)
18. **Content pipelines (text / image / audio / video / 3D)** (§50.9, §50.15)
19. **Research breadth** (§50.10)
20. **Emergency + safety** (§36.13, §50.25)

**Nothing of consequence identified as still missing from a truly universal AI-assistant coverage perspective.** Items deliberately 🚫 OUT (§53.20) are deferred for well-justified reasons (hardware, legal, or domain-expert-only scope).

**Plan is ready for user approval.** On sign-off → coding starts **Phase 0 only** per §52.

**End of planning document.**
