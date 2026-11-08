# JARVIS Changelog

Append-only. Semver-flavored. User-authored commits land here only after the user authorizes them.

## [Unreleased]

### Added
- `[TASK_LIFECYCLE]` protocol section in `core/prompt.txt` — enforces RECEIVE → EXECUTE → MONITOR → VERIFY → NOTIFY lifecycle with unprompted completion notification format.
- `core/logging_setup.py` — structured JSON logger with trace-id contextvar, rotating file handler at `logs/jarvis.log`, `JARVIS_LOG_LEVEL` env override.
- Dashboard `/health` endpoint — returns uptime, client count, token count.
- Per-IP token-bucket rate limiter on `/api/*` (60 burst / 10 rps) and `/login` (10 burst / 1 rps). No new dependency.
- Project docs: `JARVIS_PROJECT_AUDIT.md`, `JARVIS_FEATURE_MATRIX.csv`, `JARVIS_IMPLEMENTATION_PLAN.md`, `JARVIS_DEVELOPMENT_LOG.md`, `JARVIS_TEST_REPORT.md`, `JARVIS_SETUP_GUIDE.md`, this changelog.

### Added (P1 autonomous engine)
- `core/tasks.py` schema: `steps.timeout_s`, `steps.duration_s`, `tasks.idempotency_key` (additive migrations with partial unique index).
- `core/worker.py`: dispatches now honor `timeout_s` via `asyncio.wait_for`; timeout → FAILED with "timeout" error; duration recorded on every terminal step.
- `core/notify.py` (new, 240 lines): `NotifyQueue` — SQLite-backed, retry with backoff (5s / 30s / 2min, 4 attempts), per-dedup-key idempotency, pluggable sinks. Ships `JournalNotifier`, `TerminalNotifier` (macOS osascript toast), `WebhookNotifier` (opt-in).

### Tests
- +14 observability tests (`test_logging_setup.py`, `test_dashboard_rate_limit.py`). 511 → 525.
- +8 P1.3 step-timeout tests (`test_task_timeout.py`). 525 → 533.
- +5 P1.2 idempotency tests (`test_task_idempotency.py`). 533 → 538.
- +12 P1.1 / P1.6 notify tests (`test_notify.py`). 538 → 550.
- +6 P1.4 resource gate tests (`test_resource_gate.py`). 550 → 556.
- +6 P1.5 event-driven worker tests (`test_worker_event.py`). 556 → 562.
- +4 notify-queue → worker hookup tests (`test_worker_notify_hookup.py`). 562 → 566.
- +5 P2.1 memory dedup (`test_memory_dedup.py`). 566 → 571.
- +13 P2.2 scheduler (`test_scheduler.py`). 571 → 584.
- +3 P2.3 PDF (`test_pdf_gen.py`, 3 skipped without reportlab). 584 → 584.
- +7 P2.4 file dedupe (`test_file_dedupe.py`). 584 → 594 (+10 net incl prior pdf tests).
- +8 P2.5 file watcher (`test_file_watcher.py`). 594 → 602.
- +6 P2.6 killswitch (`test_killswitch.py`). 602 → 608.
- **Running total through P2: 511 → 608 green, 3 skipped (+97).**
- +12 P3.1 habit tracker (`test_habit.py`). 608 → 620.
- +9 P3.2 productivity analytics (`test_productivity_report.py`). 620 → 629.
- +6 P3.3 v1 task endpoints (`test_dashboard_tasks_v1.py`). 629 → 635.
- +3 P3.4 metrics endpoint (`test_dashboard_metrics.py`). 635 → 638.
- +6 P3.5 responsive dashboard (`test_dashboard_responsive.py`). 638 → 644.
- +9 P3.6 downloader (`test_downloader.py`). 644 → 653.
- **Running total through P3: 511 → 653 green, 3 skipped (+142). P1 + P2 + P3 (non-blocked) complete.**
- +14 P4.9 webhooks (`test_webhooks.py`). 653 → 667.
- +6 P4.10 net monitor (`test_net_monitor.py`). 667 → 673.
- +6 P4.11 predictor (`test_predictor.py`). 673 → 679.
- **Running total through P4 non-blocked: 511 → 679 green, 3 skipped (+168).**

### Added (P4 non-blocked)
- `core/webhooks.py` (new): HMAC-SHA256 signed inbound webhook registry.
- `dashboard/server.py`: `POST /webhook/{name}` route.
- `actions/net_monitor.py` (new): TCP canary probe + latency report.
- `core/predictor.py` (new): journal-driven cron suggestion heuristic.

### Deferred (cred-blocked, no autonomous landing)
- P4.1 WhatsApp (Twilio key), P4.2 Telegram (bot token), P4.3 Gmail, P4.4 GCal, P4.5 GDrive (Google OAuth), P4.6 MS 365 (Azure app), P4.7 Home Assistant (URL + token), P4.8 Voice auth (speaker model + enrollment).

### Added (P3)
- `actions/habit.py` (new): daily/weekly/every:N cadence, idempotent mark_done, streak walker.
- `actions/productivity_report.py` (new): deterministic rollup over tasks + commitments + habits.
- `dashboard/server.py`: `/api/v1/tasks/progress`, `/api/v1/tasks/history`, `/api/v1/tasks/{id}`, `/api/v1/metrics` endpoints + `_summarise_task` helper.
- `dashboard/static/app.html`: 640px + 420px media queries, iOS safe-area, table horizontal scroll.
- `actions/downloader.py` (new): http(s) URL → UPLOADS_DIR with size cap and filename sanitization.

### Added (P2)
- `memory/memory_manager.py`: sibling-key dedup in `_recursive_update` (category-scoped).
- `core/scheduler.py` (new): stdlib cron + interval + one-shot schedules; SQLite persistent; integrates with P1.2 idempotency.
- `actions/pdf_gen.py` (new): markdown → PDF via `reportlab` (optional dep; graceful fallback).
- `actions/file_dedupe.py` (new): size-bucket + sha256 duplicate scan; read-only reporting.
- `actions/file_watcher.py` (new): stdlib polling watcher; journal sink by default.
- `core/killswitch.py` (new): latched emergency stop with flag-file, programmatic trigger, listener hooks; worker tick honors it.

### Added (P1 completion)
- `core/resource_gate.py` (new): `ResourceGate` + sentinel `OPEN_GATE`. Env overrides `JARVIS_CPU_CEILING`, `JARVIS_RAM_CEILING`.
- `core/tasks.py`: `subscribe` / `unsubscribe` / `_notify_subscribers` emitter fired on every state mutation.
- `core/worker.py`: constructor gains `resource_gate` and `notify_queue` params; `run()` subscribes a thread-safe nudge that replaces fixed-interval polling; `_maybe_finalise_task` enqueues terminal transitions with dedup key `task:<id>:<status>`.
