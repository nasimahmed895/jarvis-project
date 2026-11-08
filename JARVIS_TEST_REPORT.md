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

## Latest full run

- Command: `python3 -m pytest`
- Result: **550 passed, 3 warnings in 16.99s**
- Warnings: deprecation notices from `google.genai.types` and `pynvml` (upstream; no action).

## Standing invariants

- Any new implementation chunk must leave the suite green.
- Any new module must land with at least one test in the same chunk.
- Failing tests are never marked xfail / skipped as a shortcut; root cause is addressed.
