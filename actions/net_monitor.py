"""
actions/net_monitor.py — internet connectivity probe (P4.10).

Lightweight check: open a TCP socket to each canary host:port and measure
round-trip latency. No HTTP, no DNS lookup if the user supplied an IP, no
third-party dependencies. Returns a structured report plus a human line.

Defaults chosen to be:
    - owned by high-availability providers so false negatives are rare,
    - evenly distributed so a regional outage still leaves other probes
      reachable,
    - cheap enough to run on a short interval from the dashboard without
      becoming the dashboard's biggest allocation.
"""
from __future__ import annotations

import socket
import time
from typing import Any

_DEFAULT_PROBES: tuple[tuple[str, int], ...] = (
    ("1.1.1.1",      443),   # Cloudflare
    ("8.8.8.8",      443),   # Google
    ("9.9.9.9",      443),   # Quad9
)

_DEFAULT_TIMEOUT_S = 2.0


def check(hosts: tuple[tuple[str, int], ...] = _DEFAULT_PROBES,
          timeout_s: float = _DEFAULT_TIMEOUT_S) -> dict:
    """Probe every (host, port) in parallel-ish (serially fine at N=3).
    Return `{online, probes:[{host,port,ok,latency_ms,error}], avg_ms,
    checked_at}`."""
    rows: list[dict] = []
    reachable_latencies: list[float] = []
    for host, port in hosts:
        row = _probe_one(host, port, timeout_s)
        rows.append(row)
        if row["ok"] and row["latency_ms"] is not None:
            reachable_latencies.append(row["latency_ms"])
    online = any(r["ok"] for r in rows)
    avg = (round(sum(reachable_latencies) / len(reachable_latencies), 1)
           if reachable_latencies else None)
    return {
        "online":     online,
        "probes":     rows,
        "avg_ms":     avg,
        "checked_at": time.time(),
    }


def _probe_one(host: str, port: int, timeout_s: float) -> dict:
    start = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            latency_ms = round((time.monotonic() - start) * 1000, 1)
        return {"host": host, "port": port, "ok": True,
                "latency_ms": latency_ms, "error": None}
    except OSError as e:
        return {"host": host, "port": port, "ok": False,
                "latency_ms": None, "error": f"{type(e).__name__}: {e}"}


def render(report: dict) -> str:
    if not report["probes"]:
        return "No probes configured."
    ok_count = sum(1 for r in report["probes"] if r["ok"])
    total = len(report["probes"])
    status = "ONLINE" if report["online"] else "OFFLINE"
    header = f"{status} — {ok_count}/{total} canaries reachable"
    if report["avg_ms"] is not None:
        header += f"; avg {report['avg_ms']}ms"
    lines = [header]
    for r in report["probes"]:
        if r["ok"]:
            lines.append(f"  ✓ {r['host']}:{r['port']} — {r['latency_ms']}ms")
        else:
            lines.append(f"  ✗ {r['host']}:{r['port']} — {r['error']}")
    return "\n".join(lines)


def _tool_entry(parameters: dict, **_kw: Any) -> str:
    timeout = float(parameters.get("timeout_s", _DEFAULT_TIMEOUT_S))
    custom = parameters.get("hosts")
    probes = _DEFAULT_PROBES
    if custom:
        try:
            probes = tuple((str(h["host"]), int(h["port"])) for h in custom)
        except (KeyError, TypeError, ValueError):
            return "`hosts` must be a list of {host, port} objects."
    return render(check(probes, timeout))


TOOL = {
    "name": "net_monitor",
    "description": (
        "Probe internet reachability. Attempts a TCP connect to a small "
        "list of canary hosts (Cloudflare / Google / Quad9 by default) and "
        "reports status + average latency. Use for 'am I online?' / "
        "'is the network slow?' and as a dashboard health input."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "timeout_s": {"type": "number"},
            "hosts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "host": {"type": "string"},
                        "port": {"type": "integer"},
                    },
                    "required": ["host", "port"],
                },
            },
        },
    },
    "run": _tool_entry,
}
