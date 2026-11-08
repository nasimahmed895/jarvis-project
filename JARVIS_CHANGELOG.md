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
- **Running total: 511 → 550 green (+39).**
