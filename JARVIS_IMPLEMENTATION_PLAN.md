# JARVIS Implementation Plan

**Date:** 2026-11-08
**Source audit:** `JARVIS_PROJECT_AUDIT.md`
**Baseline tests:** 525 green

Implementation proceeds P1 → P2 → P3 → P4. Items within each priority execute in the order listed. After every item: pytest must stay green, matrix row updated, development log appended.

---

## Design principles

1. **Additive, not disruptive.** Existing modules keep their shape; new behavior arrives as new files or new fields.
2. **Reuse over rewrite.** Every new feature wires through an existing utility where one exists (see Reuse Map below).
3. **Credentials never on disk in plaintext.** Every new integration flows through `core/secrets.py`.
4. **Approval for high-impact.** External sends, financial actions, destructive file ops route through `core/confirm.py`.
5. **Feature-flag anything that touches the hot worker loop.** Default OFF, flipped ON once tests cover both branches.
6. **Schema migrations are additive.** `ALTER TABLE ADD COLUMN IF NOT EXISTS`; a `schema_version` table records applied versions.

## Reuse map

| Need | Existing utility |
|---|---|
| Persistent SQLite | `core/tasks.py:_init_db` pattern + `_DB_LOCK` RLock |
| Async worker | `core/worker.py:TaskWorker.run` |
| Approval gate | `core/confirm.py:request()` + resolve callback |
| Encryption | `core/secrets.py:encrypt_bytes` (Fernet) |
| Structured events | `core/journal.py:write()` |
| Logger | `core/logging_setup.py:get_logger()` + `with_trace_id()` |
| Memory read/write | `memory/memory_manager.py:update_memory / search_memory` |
| Tool registration | `actions/*.py` export `TOOL = {...}` → `core/action_loader.py` discovery |
| Dashboard endpoint | `dashboard/server.py:_build_app` `@app.get/.post("/api/...")` + `_auth(req)` |
| Rate limit policy | `dashboard/server.py:_RateLimiter.POLICIES` (add scopes here) |

## Risk register

| Risk | Mitigation |
|---|---|
| Regression in `core/worker.py` breaks every task | Pre-flight `git tag jarvis-pre-p1-$(date +%Y%m%d)` before any P1 worker edit; every P1 worker change gated by env flag defaulted OFF |
| SQLite schema drift between environments | Migration runner records applied versions; `ALTER TABLE ADD COLUMN IF NOT EXISTS` only |
| Credential leakage in new logs | JSON logger filters known key names (gemini_api_key, slack_token, etc.) before emit |
| Dashboard API contract break | New endpoints land under new paths (`/api/v1/*` for new) — existing clients unaffected |
| New dep breaks CI | Every new `requirements.txt` entry pinned with upper bound; guarded `try: import` for optional ones |

---

## Priority 1 — Autonomous engine completion

### P1.3 — Step timeout + duration tracking

**Spec:** Each step gets a `timeout_s INTEGER` and `duration_s REAL` field. Worker wraps `_execute_step` in `asyncio.wait_for(timeout_s)`. On timeout: status=FAILED, error="timeout". Record `duration_s` on every finished step.

**Files touched:**
- `core/tasks.py` — schema add columns + accessor helpers.
- `core/worker.py` — wrap execution with `wait_for`, record duration.
- `tests/test_task_timeout.py` — new.

**Acceptance:**
1. Step with `timeout_s=1` and a `2.0s sleep` fails with status FAILED and error containing "timeout".
2. `duration_s` is populated for both success and failure paths.
3. Existing step execution (no timeout set) behaves identically to before.

### P1.2 — Duplicate execution prevention

**Spec:** Add `idempotency_key TEXT` on `tasks` with a UNIQUE index. `create_task(goal, key=...)` returns the existing task id if a row with the same key already exists in `running / completed` state.

**Files:** `core/tasks.py` + new `tests/test_task_idempotency.py`.

**Acceptance:**
1. Two `create_task(..., key="X")` calls return the same task id.
2. A completed key still returns the original id (no re-creation).
3. Different keys always create distinct tasks.

### P1.1 — Reliable notification delivery

**Spec:** New `core/notify.py` — `NotifyQueue` with SQLite persistence (`notifications.sqlite`), exponential backoff retry (5s / 30s / 2min / give-up), dedup key per notification, pluggable sinks. Hook into `core/worker.py:_notify` and the `[TASK_LIFECYCLE]` completion path.

**Files:** `core/notify.py` + `core/worker.py` hook + `tests/test_notify_queue.py`.

**Acceptance:**
1. Queue 1 notification, kill process, reopen DB, `drain()` delivers exactly once.
2. Sink raises → retried with backoff up to 3 attempts, then marked `dead_letter` with reason.
3. Same dedup key enqueued twice → single row.

### P1.6 — Notification sinks (initial set)

**Spec:** Three concrete sinks implementing a `Notifier` protocol: `TerminalNotifier` (macOS `osascript display notification`, cross-platform fallback = stderr), `JournalNotifier` (writes to `core/journal`), `WebhookNotifier` (POST JSON to a URL from env). Plug into P1.1 queue.

**Files:** `core/notify.py` (same module) + `tests/test_notifiers.py`.

**Acceptance:** Each sink has a mock-transport test; queue delivers to all registered sinks.

### P1.4 — Resource-aware scheduling

**Spec:** Worker checks `psutil.cpu_percent(interval=None)` and free RAM before dequeuing each new step. Threshold env-configurable (`JARVIS_CPU_CEILING=85`). When above, defers dequeue for one tick.

**Files:** `core/worker.py` + new `core/resource_gate.py` helper + `tests/test_resource_gate.py`.

**Acceptance:** Synthetic high-CPU (patched psutil) → `TaskWorker.ready_steps()` yields `[]`. Normal CPU → yields step.

### P1.5 — Event-driven monitoring

**Spec:** Replace fixed 0.5s poll with `asyncio.Event` wake on `create_task` / `set_step_status`. Keep 0.5s fallback timeout for safety.

**Files:** `core/worker.py` + `core/tasks.py` (emit event) + `tests/test_worker_event.py`.

**Acceptance:** From `create_task` call to first step dispatch < 50ms wall-clock in test.

---

## Priority 2 — Memory, voice, automation polish

### P2.1 — Duplicate memory detection
`memory/memory_manager.py`: compute sha1 content hash; on update_memory, if hash matches an existing row and category match, merge (ref-count + timestamp update) instead of insert.

### P2.2 — Scheduled automation
New `core/scheduler.py` using the stdlib — no new dep. Persists cron entries to `schedule.sqlite`; a background task wakes every 30s and emits `create_task` for matured rows. Exposes `actions/schedule.py` tool: `schedule(action="add", when="0 9 * * MON-FRI", goal="morning_briefing")`.

### P2.3 — PDF generation
Add `to_pdf` subaction in `actions/file_processor.py`. Try `reportlab` import; document the optional dep in `requirements.txt` (unpinned, gated). Markdown → PDF via a minimal reportlab flowable builder.

### P2.4 — Duplicate file detection
New `actions/file_dedupe.py` tool: sha256 scan of a directory tree (with size pre-filter), returns groups sorted by size × count. Reuses `confirm` for any delete action.

### P2.5 — File change monitoring
New `actions/file_watcher.py` wrapping `watchdog` (optional). Register path + glob; events publish into `core/journal.py` + optional task spawn.

### P2.6 — Emergency stop
New `core/killswitch.py`: watches a flag file `~/.jarvis_killswitch`, listens for a global hotkey (reuses `core/hotkey.py` mechanism), exposes a dashboard button (`POST /api/killswitch`). On trigger: cancels all running tasks, closes Playwright contexts, interrupts TTS mid-utterance, prints a final journal entry.

---

## Priority 3 — Productivity, dashboard, specialized

### P3.1 — Habit tracker
Extend `core/commitments.py` with a `habit` kind + `cadence` + `streak` + `last_done_at` columns. New `actions/habit.py` tool: `add / mark_done / list / streak`.

### P3.2 — Productivity analytics
Add `report(kind="productivity_weekly")` subaction in `actions/report_gen.py`: rolls up `tasks`, `commitments`, `journal` into a weekly report with charts rendered as ASCII (no new UI dep).

### P3.3 — Task progress + history dashboard
New endpoints under `/api/v1/tasks/*`: `progress`, `history`, `by_id/{n}`. Add a live task lane in `dashboard/static/app.html` that polls `progress` every 2s.

### P3.4 — System health panel
New endpoint `/api/v1/metrics` exposing CPU / RAM / disk via `psutil`; UI sparkline panel in app.html.

### P3.5 — Mobile responsive dashboard
Viewport meta + CSS breakpoints at 480 / 768 / 1024 in `app.html`; collapsible sidebar on <768px.

### P3.6 — Browser downloads
In `actions/browser_control.py`, add `download` action using Playwright's `expect_download()`; save to `UPLOADS_DIR` and return path.

### P3.7 — Voice personalization (preset library expansion)
Document voice pack layout in setup guide; add `voice_list` subaction returning available voices per language. Full language-specific voice shipping remains BLOCKED on external voice pack.

---

## Priority 4 — Integrations, smart home, advanced intel

Each P4 item follows the same pattern:
- New action/plugin module.
- Credential sourced from `core/secrets.py` (keyring-backed).
- First real send/post gated by `core/confirm.py`.
- Behavior behind a `try: import optional_dep` guard; action returns an informative error when the dep or credential is missing.

| # | Item | Module | Blocker |
|---|---|---|---|
| P4.1 | WhatsApp send (Twilio) | `actions/whatsapp.py` | Twilio SID/Token |
| P4.2 | Telegram bot | `plugins/telegram_bot/` | Bot token |
| P4.3 | Gmail | `actions/gmail.py` | Google OAuth creds |
| P4.4 | Google Calendar | `actions/gcal.py` | Same |
| P4.5 | Google Drive | `actions/gdrive.py` | Same |
| P4.6 | Microsoft 365 Graph | `actions/ms365.py` | Azure app |
| P4.7 | Home Assistant (whole CAT13) | `actions/smart_home.py` | HA URL + long-lived token |
| P4.8 | Voice auth | `core/speaker_id.py` | Enrollment flow + model |
| P4.9 | Incoming webhooks | dashboard route `/webhook/{name}` | — |
| P4.10 | Internet connection monitor | `actions/net_monitor.py` | — |
| P4.11 | Predictive task suggestions | `core/predictor.py` | — |

Items P4.9 / P4.10 / P4.11 have no credential blocker and can ship whenever their place in the queue arrives.

---

## Items already blocked (acknowledged, not scheduled)

- **CAT4.8 Mobile push** — needs Firebase/APNs project; user must provision.
- **Live payments SDK** — hard-refused in `core/payments.py` by design; user consent required.
- **Bangla TTS voice pack** — upstream dependency.

---

## Verification loop (applies to every item)

1. `python3 -m pytest` — the 525-baseline must stay green after each change.
2. New tests run individually with `-v`.
3. For dashboard changes: start the dashboard module, curl the endpoint, confirm shape + rate-limit behavior.
4. For worker changes: create a disposable task via REPL, watch `core/journal` + `logs/jarvis.log`, confirm SQLite state transitions.
5. Update `JARVIS_FEATURE_MATRIX.csv` row for the touched feature.
6. Append an entry to `JARVIS_DEVELOPMENT_LOG.md`.
7. Pause before any commit; user authorizes.

---

## Execution starts

Next action is P1.3 (step timeout). After it lands and tests go green, P1.2 immediately follows.
