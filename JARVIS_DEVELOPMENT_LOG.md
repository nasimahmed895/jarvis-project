# JARVIS Development Log

Append-only. One entry per implementation chunk. Format:

    ## YYYY-MM-DD — <short title>
    - What: <single-sentence description>
    - Files: <list>
    - Tests: <baseline → new>
    - Matrix updates: <feature ids>
    - Notes: <anything the next reader needs>

---

## 2026-11-08 — [TASK_LIFECYCLE] protocol added to system prompt
- What: appended the autonomous task monitoring protocol block to `core/prompt.txt`, condensed to match existing voice.
- Files: `core/prompt.txt`.
- Tests: no change (525 baseline).
- Matrix updates: 2.13, 4.1, 4.2, 4.3.
- Notes: completion format is now part of the model's own instructions; downstream notification work (P1.1 / P1.6) will extend this contract.

## 2026-11-08 — Observability + rate limit landing
- What: added structured JSON logger and dashboard `/health` endpoint; added per-IP rate limiter on `/api/*` and `/login`.
- Files: `core/logging_setup.py` (new), `dashboard/server.py`, `tests/test_logging_setup.py` (new), `tests/test_dashboard_rate_limit.py` (new).
- Tests: 511 → 525 green (+14).
- Matrix updates: 14.6, 15.5, 15.7, 14.12.
- Notes: no new deps — slowapi was not available, replaced with in-process token-bucket. Logger activates on-demand; `JARVIS_LOG_LEVEL` overrides level.

## 2026-11-08 — P1.3 step timeout + duration tracking
- What: added `timeout_s` and `duration_s` columns on `steps`; worker wraps dispatch in `asyncio.wait_for` when `timeout_s` set; `set_step_status` records duration on terminal transitions.
- Files: `core/tasks.py`, `core/worker.py`, `tests/test_task_timeout.py` (new).
- Tests: 525 → 533 (+8).
- Matrix updates: 20.8 MISSING → IMPLEMENTED.
- Notes: migration is additive (`ALTER TABLE ADD COLUMN`); unspecified `timeout_s` preserves pre-change behavior exactly.

## 2026-11-08 — P1.1 + P1.6 reliable notification delivery
- What: new `core/notify.py` — `NotifyQueue` persistent to SQLite with exponential backoff (5s / 30s / 2min, 4 attempts) and dedup keys. Three ready-to-use sinks: `JournalNotifier`, `TerminalNotifier` (macOS osascript toast fallback), `WebhookNotifier` (opt-in POST).
- Files: `core/notify.py` (new), `tests/test_notify.py` (new).
- Tests: 538 → 550 (+12).
- Matrix updates: 20.12 PARTIAL → IMPLEMENTED, 4.12 NEEDS_TESTING → IMPLEMENTED, 4.7 PARTIAL → IMPLEMENTED (desktop toast via TerminalNotifier).
- Notes: not yet wired into `core/worker.py:_notify` — the queue is standalone and tested; next hookup chunk will route task-completion events through it. Dedup keys use the shape `task:<id>:<kind>` by convention.

## 2026-11-08 — P1.2 duplicate execution prevention
- What: added `idempotency_key` column on `tasks` with a partial-unique index; `create_task(..., idempotency_key=...)` returns the existing task id when the key matches, in any state (planned/running/done/failed/cancelled).
- Files: `core/tasks.py`, `tests/test_task_idempotency.py` (new).
- Tests: 533 → 538 (+5).
- Matrix updates: 20.11 MISSING → IMPLEMENTED.
- Notes: NULL keys are not considered duplicates (partial index); old call sites that omit the parameter are unaffected.

## 2026-11-08 — Audit + plan docs produced
- What: ran 3 parallel Explore passes across all 20 feature categories; wrote `JARVIS_PROJECT_AUDIT.md`, `JARVIS_FEATURE_MATRIX.csv`, `JARVIS_IMPLEMENTATION_PLAN.md`.
- Files: project-root docs (new).
- Tests: no code change.
- Matrix updates: n/a — matrix created in this step.
- Notes: audit identifies ~155 implemented / 22 partial / 28 missing / 5 needs-testing / 2 blocked across 212 features. Execution order: P1.3 → P1.2 → P1.1 → P1.6 → P1.4 → P1.5 → P2.*.
