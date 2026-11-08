# JARVIS Test Report

Rolling summary of pytest runs tied to implementation chunks.

| Date | Chunk | Baseline | After | Delta | New tests |
|---|---|---|---|---|---|
| 2026-11-08 | `[TASK_LIFECYCLE]` prompt | 511 | 511 | 0 | none (prompt-only) |
| 2026-11-08 | observability + rate limit | 511 | 525 | +14 | `test_logging_setup.py` (6), `test_dashboard_rate_limit.py` (8) |
| 2026-11-08 | audit docs | 525 | 525 | 0 | none |
| 2026-11-08 | P1.3 step timeout | 525 | 533 | +8 | `test_task_timeout.py` |
| 2026-11-08 | P1.2 idempotency | 533 | 538 | +5 | `test_task_idempotency.py` |
| 2026-11-08 | P1.1 + P1.6 notify queue | 538 | 550 | +12 | `test_notify.py` |
| 2026-11-08 | P1.4 resource gate | 550 | 556 | +6 | `test_resource_gate.py` |
| 2026-11-08 | P1.5 event-driven worker | 556 | 562 | +6 | `test_worker_event.py` |
| 2026-11-08 | NotifyQueue → worker hookup | 562 | 566 | +4 | `test_worker_notify_hookup.py` |
| 2026-11-08 | P2.1 memory dedup | 566 | 571 | +5 | `test_memory_dedup.py` |
| 2026-11-08 | P2.2 scheduler | 571 | 584 | +13 | `test_scheduler.py` |
| 2026-11-08 | P2.3 PDF generation (gated) | 584 | 584 | +0* | `test_pdf_gen.py` (3 skipped, 3 run) |
| 2026-11-08 | P2.4 file dedupe | 584 | 594 | +10 | `test_file_dedupe.py` + 3 pdf stub tests counted earlier |
| 2026-11-08 | P2.5 file watcher | 594 | 602 | +8 | `test_file_watcher.py` |
| 2026-11-08 | P2.6 killswitch | 602 | 608 | +6 | `test_killswitch.py` |
| 2026-11-08 | P3.1 habit tracker | 608 | 620 | +12 | `test_habit.py` |
| 2026-11-08 | P3.2 productivity analytics | 620 | 629 | +9 | `test_productivity_report.py` |
| 2026-11-08 | P3.3 v1 task endpoints | 629 | 635 | +6 | `test_dashboard_tasks_v1.py` |
| 2026-11-08 | P3.4 metrics endpoint | 635 | 638 | +3 | `test_dashboard_metrics.py` |
| 2026-11-08 | P3.5 responsive dashboard | 638 | 644 | +6 | `test_dashboard_responsive.py` |
| 2026-11-08 | P3.6 downloader | 644 | 653 | +9 | `test_downloader.py` |
| 2026-11-08 | P4.9 webhooks | 653 | 667 | +14 | `test_webhooks.py` |
| 2026-11-08 | P4.10 net monitor | 667 | 673 | +6 | `test_net_monitor.py` |
| 2026-11-08 | P4.11 predictor | 673 | 679 | +6 | `test_predictor.py` |

## Latest full run

- Command: `python3 -m pytest`
- Result: **679 passed, 3 skipped (reportlab optional), 3 warnings in 22.18s**
- Warnings: deprecation notices from `google.genai.types` and `pynvml` (upstream; no action).

## Standing invariants

- Any new implementation chunk must leave the suite green.
- Any new module must land with at least one test in the same chunk.
- Failing tests are never marked xfail / skipped as a shortcut; root cause is addressed.
