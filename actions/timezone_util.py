"""
Timezone utility — conversions and shared-working-window finder.

Three operations:

  convert   — translate a datetime from one zone to another
  now       — current time in a named zone
  windows   — find working-hour windows across several attendees'
              zones; returns the overlapping slots in UTC (and in each
              attendee's local time for display)

Uses the stdlib ``zoneinfo`` module (Python 3.9+). No external API, no
tz database download — the zone data ships with the system / Python.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except Exception:  # pragma: no cover — Python 3.8 or no tzdata
    ZoneInfo = None  # type: ignore[assignment]
    ZoneInfoNotFoundError = Exception  # type: ignore[assignment]


# Short-form aliases people say out loud, mapped to canonical IANA names.
# Kept intentionally small — the model can hand us the full name when it
# knows it; this table just rescues the common abbreviations.
_ALIAS = {
    "utc":   "UTC",
    "gmt":   "UTC",
    "pst":   "America/Los_Angeles",
    "pdt":   "America/Los_Angeles",
    "pt":    "America/Los_Angeles",
    "mst":   "America/Denver",
    "mt":    "America/Denver",
    "cst":   "America/Chicago",
    "ct":    "America/Chicago",
    "est":   "America/New_York",
    "edt":   "America/New_York",
    "et":    "America/New_York",
    "bst":   "Europe/London",
    "cet":   "Europe/Paris",
    "cest":  "Europe/Paris",
    "eet":   "Europe/Helsinki",
    "ist":   "Asia/Kolkata",   # Indian Standard Time
    "sgt":   "Asia/Singapore",
    "hkt":   "Asia/Hong_Kong",
    "jst":   "Asia/Tokyo",
    "kst":   "Asia/Seoul",
    "aest":  "Australia/Sydney",
    "aedt":  "Australia/Sydney",
    "nzst":  "Pacific/Auckland",
    "nzdt":  "Pacific/Auckland",
    "brt":   "America/Sao_Paulo",
    "art":   "America/Argentina/Buenos_Aires",
    "dhaka": "Asia/Dhaka",     # common colloquial reference
    "bst":   "Europe/London",  # intentional duplicate alias
}


def _resolve_zone(raw: str):
    """Resolve to a canonical IANA zone. The alias table is consulted
    BEFORE the raw ZoneInfo call so a lowercase short name like ``gmt``
    comes back as the canonical ``UTC`` — tzdata on some platforms will
    accept ``gmt`` verbatim and return it as ``gmt``, which produces
    inconsistent output across machines."""
    if ZoneInfo is None:
        raise RuntimeError("zoneinfo unavailable; upgrade to Python 3.9+")
    if not raw:
        raise ValueError("timezone required")
    s = raw.strip()
    canon = _ALIAS.get(s.lower())
    if canon:
        return ZoneInfo(canon)
    try:
        return ZoneInfo(s)
    except ZoneInfoNotFoundError:
        raise ValueError(f"unknown timezone {raw!r}")


def _parse_dt(raw: str, tz) -> datetime:
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d"):
        try:
            dt = datetime.strptime(raw, fmt)
            return dt.replace(tzinfo=tz)
        except ValueError:
            continue
    raise ValueError(f"could not parse datetime {raw!r}")


def _convert(when_raw: str, from_zone: str, to_zone: str) -> dict:
    src = _resolve_zone(from_zone)
    dst = _resolve_zone(to_zone)
    src_dt = _parse_dt(when_raw, src)
    dst_dt = src_dt.astimezone(dst)
    return {
        "source": {
            "zone": str(src),
            "time": src_dt.strftime("%Y-%m-%d %H:%M %Z"),
        },
        "target": {
            "zone": str(dst),
            "time": dst_dt.strftime("%Y-%m-%d %H:%M %Z"),
        },
        "delta_hours": round(
            (src_dt.utcoffset() - dst_dt.utcoffset()).total_seconds() / 3600.0
            if src_dt.utcoffset() and dst_dt.utcoffset() else 0.0,
            2,
        ),
    }


_ZONE_SPEC = re.compile(
    r"^\s*(?P<zone>[^,|]+?)(?:[:@]\s*(?P<start>\d{1,2}))(?:[-–]\s*(?P<end>\d{1,2}))?\s*$"
)


def _parse_attendees(raw) -> list[dict]:
    """Parse a specifier like::

        Asia/Tokyo:9-18, Europe/London:9-18, America/Los_Angeles:9-17

    Any entry without hours uses a 9-17 default. Accepts JSON array of
    dicts too: ``[{"zone": "...", "start_hour": 9, "end_hour": 17}]``.
    """
    if isinstance(raw, list):
        out = []
        for item in raw:
            if isinstance(item, dict):
                z = str(item.get("zone") or "").strip()
                if not z:
                    continue
                out.append({
                    "zone": z,
                    "start_hour": int(item.get("start_hour") or 9),
                    "end_hour":   int(item.get("end_hour") or 17),
                })
        return out
    if isinstance(raw, str):
        s = raw.strip()
        if s.startswith("["):
            try:
                return _parse_attendees(json.loads(s))
            except Exception:
                pass
        out = []
        for part in re.split(r"[|,]+", s):
            m = _ZONE_SPEC.match(part)
            if not m:
                part = part.strip()
                if part:
                    out.append({"zone": part, "start_hour": 9, "end_hour": 17})
                continue
            start = int(m.group("start") or 9)
            end = int(m.group("end") or 17)
            out.append({"zone": m.group("zone").strip(),
                        "start_hour": start, "end_hour": end})
        return out
    return []


def _overlap_windows(attendees: list[dict], *, duration_min: int = 30,
                     days: int = 5,
                     start: datetime | None = None) -> list[dict]:
    """Hour-granularity sweep: for each UTC hour across the next ``days``
    days, check whether EVERY attendee is within their working hours.
    Returns the start-of-overlap blocks (minimum ``duration_min`` long)."""
    if not attendees:
        return []
    zones = [( _resolve_zone(a["zone"]), int(a["start_hour"]),
               int(a["end_hour"]) ) for a in attendees]
    start_utc = (start or datetime.now(tz=timezone.utc)
                 .replace(minute=0, second=0, microsecond=0))
    end_utc = start_utc + timedelta(days=days)

    good: list[datetime] = []
    cur = start_utc
    while cur < end_utc:
        if all(_within_hours(cur, z, sh, eh) for z, sh, eh in zones):
            good.append(cur)
        cur += timedelta(hours=1)

    # Merge consecutive hours into contiguous windows.
    windows: list[dict] = []
    i = 0
    while i < len(good):
        j = i
        while (j + 1 < len(good) and
               good[j + 1] - good[j] == timedelta(hours=1)):
            j += 1
        block_start = good[i]
        block_end = good[j] + timedelta(hours=1)
        if (block_end - block_start).total_seconds() / 60 >= duration_min:
            windows.append({
                "start_utc": block_start.strftime("%Y-%m-%d %H:%M UTC"),
                "end_utc":   block_end.strftime("%Y-%m-%d %H:%M UTC"),
                "per_zone":  [{
                    "zone": str(z),
                    "start": block_start.astimezone(z).strftime("%Y-%m-%d %H:%M %Z"),
                    "end":   block_end.astimezone(z).strftime("%Y-%m-%d %H:%M %Z"),
                } for z, _, _ in zones],
            })
        i = j + 1
    return windows


def _within_hours(utc_hour: datetime, z, start_hour: int, end_hour: int) -> bool:
    local = utc_hour.astimezone(z)
    # Treat weekends as off by default. A caller who wants weekend
    # meetings can pass start_hour=0, end_hour=24.
    if local.weekday() >= 5:
        return False
    return start_hour <= local.hour < end_hour


def timezone_util(
    parameters:    dict | None = None,
    response: Any = None,
    player:   Any = None,
    session_memory: Any = None,
) -> str:
    """Timezone operations.

    Actions:
      now        zone=
      convert    when=<YYYY-MM-DD HH:MM> from=<zone> to=<zone>
      windows    attendees="Tokyo:9-18, London:9-17, PT:9-16"
                 duration_min=<n>   days=<n>
    """
    if ZoneInfo is None:
        return "timezone: zoneinfo unavailable (need Python 3.9+ with tzdata)."
    p = parameters or {}
    action = str(p.get("action", "")).lower().strip()

    if action == "now":
        try:
            z = _resolve_zone(str(p.get("zone") or "UTC"))
        except ValueError as e:
            return f"timezone now: {e}"
        now = datetime.now(z)
        return f"{now.strftime('%Y-%m-%d %H:%M %Z')} ({str(z)})"

    if action == "convert":
        when = str(p.get("when") or "").strip()
        src = str(p.get("from") or "").strip()
        dst = str(p.get("to") or "").strip()
        if not (when and src and dst):
            return "timezone convert: provide 'when', 'from', 'to'."
        try:
            r = _convert(when, src, dst)
        except (ValueError, ZoneInfoNotFoundError) as e:
            return f"timezone convert: {e}"
        return (f"{r['source']['time']} ({r['source']['zone']}) "
                f"→ {r['target']['time']} ({r['target']['zone']}) "
                f"[Δ {r['delta_hours']}h]")

    if action == "windows":
        attendees = _parse_attendees(p.get("attendees"))
        if not attendees:
            return "timezone windows: provide 'attendees'."
        try:
            duration = int(p.get("duration_min") or 30)
        except (TypeError, ValueError):
            duration = 30
        try:
            days = int(p.get("days") or 5)
        except (TypeError, ValueError):
            days = 5
        try:
            windows = _overlap_windows(attendees, duration_min=duration,
                                        days=days)
        except ValueError as e:
            return f"timezone windows: {e}"
        if not windows:
            return (f"No overlapping working-hour windows across "
                    f"{len(attendees)} attendee(s) in the next "
                    f"{days} day(s).")
        lines = [f"Shared working windows (next {days}d, "
                 f"{duration}m min):"]
        for w in windows[:10]:
            lines.append(f"  - {w['start_utc']} → {w['end_utc']}")
            for z in w["per_zone"]:
                lines.append(f"      · {z['zone']}: {z['start']} → {z['end']}")
        return "\n".join(lines)

    return (f"timezone: unknown action {action!r}. "
            "Actions: now | convert | windows")


TOOL = {
    "name": "timezone",
    "description": (
        "Timezone operations. 'now' returns the current time in a "
        "named zone. 'convert' translates a wall-clock time from one "
        "zone to another. 'windows' finds overlapping working-hour "
        "windows across several attendees for scheduling meetings — "
        "pass 'attendees' as comma-separated 'Zone:start-end' entries "
        "(e.g. 'Asia/Tokyo:9-18, Europe/London:9-17, PT:9-16') or a "
        "JSON list. Accepts common short names (PT, ET, GMT, JST, IST, "
        "CET) as well as full IANA names (Asia/Tokyo). Weekends are "
        "excluded by default."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action":       {"type": "STRING", "description": "now | convert | windows"},
            "zone":         {"type": "STRING", "description": "Zone for 'now'."},
            "when":         {"type": "STRING", "description": "Datetime YYYY-MM-DD HH:MM for 'convert'."},
            "from":         {"type": "STRING", "description": "Source zone for 'convert'."},
            "to":           {"type": "STRING", "description": "Target zone for 'convert'."},
            "attendees":    {"type": "STRING", "description": "Comma-separated 'Zone:start-end' or JSON list for 'windows'."},
            "duration_min": {"type": "INTEGER", "description": "Minimum slot length in minutes (default 30)."},
            "days":         {"type": "INTEGER", "description": "Horizon in days (default 5)."},
        },
        "required": ["action"],
    },
    "handler": timezone_util,
}
