"""
core/resource_gate.py — CPU/RAM throttle for the agent worker (P1.4).

The worker dequeues ready steps on every tick. When the host is already
under load (heavy local model, full browser, concurrent user build) piling
more concurrent steps on top causes thrashing. `ResourceGate.allow()` is
the single boolean the worker checks before dequeue — False means defer
this tick without cancelling anything already inflight.

Decisions are made on `psutil.cpu_percent(interval=None)` (non-blocking;
first call after process start returns 0.0 so the gate is always open for
one bootstrap tick) and `psutil.virtual_memory().percent`. Both ceilings
are overridable via environment so an operator can tighten/loosen them
without a code change.

Env:
    JARVIS_CPU_CEILING  — percent above which to pause (default 85)
    JARVIS_RAM_CEILING  — percent above which to pause (default 90)
"""
from __future__ import annotations

import os
from typing import Callable

from core.logging_setup import get_logger

_log = get_logger(__name__)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.environ.get(name, "")
        return float(v) if v else default
    except ValueError:
        return default


class ResourceGate:
    """Non-blocking CPU/RAM gate. A fresh instance keeps no history — the
    worker calls `allow()` once per tick and defers for a single tick when
    the gate closes.

    `cpu_fn` and `ram_fn` default to psutil probes but accept injection for
    tests. They must return a percentage (0.0–100.0)."""

    def __init__(self,
                 cpu_ceiling: float | None = None,
                 ram_ceiling: float | None = None,
                 cpu_fn: Callable[[], float] | None = None,
                 ram_fn: Callable[[], float] | None = None):
        self.cpu_ceiling = (cpu_ceiling if cpu_ceiling is not None
                            else _env_float("JARVIS_CPU_CEILING", 85.0))
        self.ram_ceiling = (ram_ceiling if ram_ceiling is not None
                            else _env_float("JARVIS_RAM_CEILING", 90.0))
        self._cpu_fn = cpu_fn or _default_cpu
        self._ram_fn = ram_fn or _default_ram
        self._last_blocked_reason: str | None = None

    def allow(self) -> bool:
        cpu = self._cpu_fn()
        ram = self._ram_fn()
        if cpu > self.cpu_ceiling:
            self._last_blocked_reason = f"cpu={cpu:.1f}%>{self.cpu_ceiling:.0f}%"
            _log.info("resource_gate_blocked", extra={"reason": self._last_blocked_reason})
            return False
        if ram > self.ram_ceiling:
            self._last_blocked_reason = f"ram={ram:.1f}%>{self.ram_ceiling:.0f}%"
            _log.info("resource_gate_blocked", extra={"reason": self._last_blocked_reason})
            return False
        self._last_blocked_reason = None
        return True

    @property
    def last_blocked_reason(self) -> str | None:
        return self._last_blocked_reason


def _default_cpu() -> float:
    try:
        import psutil
        return float(psutil.cpu_percent(interval=None))
    except Exception:
        return 0.0


def _default_ram() -> float:
    try:
        import psutil
        return float(psutil.virtual_memory().percent)
    except Exception:
        return 0.0


# A sentinel always-open gate — used as the default inside TaskWorker so
# existing call sites that never heard of ResourceGate keep working.
class _OpenGate:
    cpu_ceiling = 100.0
    ram_ceiling = 100.0
    last_blocked_reason = None

    def allow(self) -> bool:
        return True


OPEN_GATE = _OpenGate()
