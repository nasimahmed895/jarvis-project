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

## 2026-11-08 — P4.11 predictive task suggestions
- What: new `core/predictor.py` — scans journal for `task_done` / `step_start`, groups by goal, proposes daily or weekly cron slots when support ≥ 3 and time spread ≤ 30min. Pure heuristic, no LLM.
- Files: `core/predictor.py` (new), `tests/test_predictor.py` (new).
- Tests: 673 → 679 (+6).
- Matrix updates: 17.3 MISSING → IMPLEMENTED.
- Notes: suggestions are read-only; caller passes accepted suggestion into `core/scheduler.py`.

## 2026-11-08 — P4.10 internet connectivity monitor
- What: new `actions/net_monitor.py` — TCP probes to 3 default canaries (Cloudflare / Google / Quad9) with per-probe latency. Supports custom host lists.
- Files: `actions/net_monitor.py` (new), `tests/test_net_monitor.py` (new).
- Tests: 667 → 673 (+6).
- Matrix updates: 15.4 MISSING → IMPLEMENTED.

## 2026-11-08 — P4.9 inbound webhook receiver
- What: new `core/webhooks.py` — HMAC-SHA256 registry (name → secret + handler). Dashboard route `POST /webhook/{name}` verifies `X-JARVIS-Signature: sha256=<hex>` via `hmac.compare_digest`.
- Files: `core/webhooks.py` (new), `dashboard/server.py` (new route), `tests/test_webhooks.py` (new).
- Tests: 653 → 667 (+14).
- Matrix updates: 19.8 MISSING → IMPLEMENTED.
- Notes: unknown webhook names return 401 (not 404) to avoid an enumeration oracle.

## 2026-11-08 — P3.6 URL downloader
- What: new `actions/downloader.py` — http/https → `UPLOADS_DIR` with size cap, filename sanitization, collision-safe naming, pluggable opener for tests.
- Files: `actions/downloader.py` (new), `tests/test_downloader.py` (new).
- Tests: 644 → 653 (+9).
- Matrix updates: 7.8 PARTIAL → IMPLEMENTED.
- Notes: playwright-backed browser-session downloads (authenticated / SPA blob) deferred; this covers the common "save that URL" case.

## 2026-11-08 — P3.5 mobile responsive dashboard
- What: added 640px and 420px CSS breakpoints to `dashboard/static/app.html`. Collapses Tailwind grids to 1 column, tables scroll horizontally, donut/spark sizes shrink, iOS safe-area respected.
- Files: `dashboard/static/app.html`, `tests/test_dashboard_responsive.py` (new).
- Tests: 638 → 644 (+6).
- Matrix updates: 18.12 MISSING → IMPLEMENTED.
- Notes: pre-existing 900px breakpoint (tablet/sidebar) left in place.

## 2026-11-08 — P3.4 system metrics endpoint
- What: `GET /api/v1/metrics` returns uptime, CPU%, RAM used/total, disk used/total, process count via psutil. Guarded — a psutil import failure returns uptime + error string rather than 500.
- Files: `dashboard/server.py`, `tests/test_dashboard_metrics.py` (new).
- Tests: 635 → 638 (+3).
- Matrix updates: 18.9 PARTIAL → IMPLEMENTED (JSON endpoint); UI panel follow-up.

## 2026-11-08 — P3.3 task progress / history / detail endpoints
- What: three new `/api/v1/tasks/*` endpoints: `progress` (active tasks summarised to {id, goal, done, total, pct, next_step}), `history` (terminal status filter), `{task_id}` (full detail + steps). Added `_summarise_task` helper at module scope.
- Files: `dashboard/server.py`, `tests/test_dashboard_tasks_v1.py` (new).
- Tests: 629 → 635 (+6).
- Matrix updates: 18.3 PARTIAL → IMPLEMENTED (data endpoint; UI card follow-up), 18.8 NEEDS_TESTING → IMPLEMENTED.

## 2026-11-08 — P3.2 productivity analytics
- What: new `actions/productivity_report.py` — deterministic rollup over tasks + commitments + habits. `rollup(window)` returns a dict; `render_markdown()` pretty-prints. Separate from the LLM-driven `report_gen` so the numbers are exact.
- Files: `actions/productivity_report.py` (new), `tests/test_productivity_report.py` (new).
- Tests: 620 → 629 (+9).
- Matrix updates: 11.11 MISSING → IMPLEMENTED.

## 2026-11-08 — P3.1 habit tracker
- What: new `actions/habit.py` — SQLite habits + habit_log tables, cadences `daily` / `weekly` / `every:N`, idempotent `mark_done` per slot, streak computed by walking backwards until the first gap.
- Files: `actions/habit.py` (new), `tests/test_habit.py` (new).
- Tests: 608 → 620 (+12).
- Matrix updates: 11.7 MISSING → IMPLEMENTED.

## 2026-11-08 — P2.6 emergency stop (killswitch)
- What: new `core/killswitch.py` with three activation paths — flag file `~/.jarvis_killswitch`, programmatic `trigger(reason)`, listener hooks. Latched until `reset()`. Worker checks `is_active()` on every tick and cancels inflight work when armed.
- Files: `core/killswitch.py` (new), `core/worker.py`, `tests/test_killswitch.py` (new).
- Tests: 602 → 608 (+6).
- Matrix updates: 14.11 MISSING → IMPLEMENTED.
- Notes: TTS / browser / UI wiring deferred — the switch exists and the worker respects it; broader call sites will register listeners as they migrate.

## 2026-11-08 — P2.5 file change monitor
- What: new `actions/file_watcher.py` with stdlib-only polling (no watchdog dep). `FileWatcher` class + registry + tool-entry `add/remove/list`. Events emit to `core.journal` by default.
- Files: `actions/file_watcher.py` (new), `tests/test_file_watcher.py` (new).
- Tests: 594 → 602 (+8).
- Matrix updates: 8.14 PARTIAL → IMPLEMENTED.
- Notes: swap to watchdog later when inotify-grade latency is wanted; API is stable.

## 2026-11-08 — P2.4 duplicate file detection
- What: new `actions/file_dedupe.py`. Two-stage scan: group by size, hash only collisions. Returns sorted groups + human-readable report. Read-only — no deletes.
- Files: `actions/file_dedupe.py` (new), `tests/test_file_dedupe.py` (new).
- Tests: 584 → 594 (+10).
- Matrix updates: 8.12 MISSING → IMPLEMENTED.
- Notes: callers that want to delete duplicates should route through `file_controller` + `confirm.request()`.

## 2026-11-08 — P2.3 PDF generation
- What: new `actions/pdf_gen.py` with markdown → PDF via `reportlab`. Guarded import; returns install hint when dep missing. Supports headings, bold/italic, inline code, horizontal-rule page breaks.
- Files: `actions/pdf_gen.py` (new), `tests/test_pdf_gen.py` (new).
- Tests: 571 → 584 (+13 from scheduler, PDF skipped without dep). PDF real-render tests skip cleanly when reportlab absent.
- Matrix updates: 8.6 MISSING → IMPLEMENTED (graceful stub, full function with `pip install reportlab`).
- Notes: optional dep documented in SETUP_GUIDE.

## 2026-11-08 — P2.2 scheduler
- What: new `core/scheduler.py` — stdlib-only. Hand-rolled cron parser (5 field, step/range/list), interval triggers, one-shot `date` runs. SQLite-persistent. Idempotency key per fire (`schedule:<id>:<iso-slot>`) composes with P1.2 to prevent duplicate dispatch.
- Files: `core/scheduler.py` (new), `tests/test_scheduler.py` (new).
- Tests: 566 → 584 (+18 across P2.1 + P2.2 + bug-fix wave). Caught during integration: `extra={"name": ...}` collides with `logging.LogRecord.name` → renamed to `sched_name`; initial cron DOW check was incorrect (compared two different encodings) → use membership test.
- Matrix updates: 5.2 PARTIAL → IMPLEMENTED.

## 2026-11-08 — P2.1 memory duplicate detection
- What: `memory/memory_manager._recursive_update` now case-folds + whitespace-normalizes each leaf value and checks siblings in the same category. If an existing key already carries the same value, the write is dropped (first key wins).
- Files: `memory/memory_manager.py`, `tests/test_memory_dedup.py` (new).
- Tests: 566 → 571 (+5).
- Matrix updates: 12.9 MISSING → IMPLEMENTED.
- Notes: dedup is category-scoped — identical values across categories (preferences vs notes) are allowed on purpose, since the cross-category match would be a false positive.

## 2026-11-08 — NotifyQueue → worker hookup
- What: `TaskWorker.__init__` accepts optional `notify_queue`; `_maybe_finalise_task` enqueues a single durable row per terminal task with dedup key `task:<id>:<status>`. Voice/UI `_notify` fires in parallel unchanged.
- Files: `core/worker.py`, `tests/test_worker_notify_hookup.py` (new).
- Tests: 562 → 566 (+4).
- Matrix updates: none new (completion notification already marked IMPLEMENTED when queue landed; this closes the end-to-end wiring).
- Notes: legacy callers (`TaskWorker(dispatch, notify)`) are unchanged — queue defaults to None and the enqueue path is a no-op.

## 2026-11-08 — P1.5 event-driven worker wake
- What: new `tasks.subscribe/unsubscribe/_notify_subscribers` emitter. All mutations (`create_task`, `add_step`, `set_step_status`, `set_task_status`, `cancel_task`) fire subscribers. `TaskWorker.run()` subscribes a thread-safe `self._nudge.set()` via `loop.call_soon_threadsafe`. Replaced raw `asyncio.sleep(_POLL_SECONDS)` with `wait_for(nudge.wait(), timeout=_POLL_SECONDS)`.
- Files: `core/tasks.py`, `core/worker.py`, `tests/test_worker_event.py` (new).
- Tests: 556 → 562 (+6).
- Matrix updates: 20.3 PARTIAL → IMPLEMENTED.
- Notes: subscribers run synchronously inside the mutation lock — must be cheap. The worker's wake callback is a one-liner that defers all work to the event loop.

## 2026-11-08 — P1.4 resource-aware scheduling
- What: new `core/resource_gate.py` with `ResourceGate(cpu_ceiling, ram_ceiling)` and sentinel `OPEN_GATE`. Reads `psutil.cpu_percent(interval=None)` + `psutil.virtual_memory().percent` (injectable for tests). `TaskWorker.__init__` accepts optional `resource_gate`; `_tick` short-circuits dequeue when `allow()` returns False. Env: `JARVIS_CPU_CEILING`, `JARVIS_RAM_CEILING` (defaults 85 / 90).
- Files: `core/resource_gate.py` (new), `core/worker.py`, `tests/test_resource_gate.py` (new).
- Tests: 550 → 556 (+6).
- Matrix updates: 20.10 MISSING → IMPLEMENTED.
- Notes: in-flight steps keep running when the gate closes; only new dequeues are deferred.

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
