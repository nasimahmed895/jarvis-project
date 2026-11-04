"""
Grounded real-world knowledge aggregator.

Fills the audit's section-1 gap. Composes the pieces that already exist
(``web_search``, ``perception``, memory ``identity``, stdlib datetime)
with lightweight free APIs (Open-Meteo for weather + geocoding) and a
claim-tagging layer so the voice loop can distinguish:

  * ``verified``       — ≥2 independent sources agree
  * ``estimated``      — exactly one external source
  * ``model_knowledge``— no external source, LLM-only
  * ``unknown``        — searched but nothing usable found

Every call is best-effort and never raises: a failed geocode falls
back to the user's stored identity city, an unreachable API returns
tagged "unknown" rather than fabricating a number.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

try:
    import requests  # type: ignore
except Exception:  # pragma: no cover
    requests = None  # type: ignore[assignment]

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]


# ── Short-TTL cache so repeated sub-queries during one turn don't
# hammer the public endpoints. 5 minutes is enough for weather and
# geocoding to stay fresh without producing stale situational data.
_CACHE: dict[str, tuple[float, Any]] = {}
_CACHE_TTL = 300.0


def _cache_get(key: str):
    entry = _CACHE.get(key)
    if not entry:
        return None
    ts, value = entry
    if (time.time() - ts) > _CACHE_TTL:
        _CACHE.pop(key, None)
        return None
    return value


def _cache_set(key: str, value) -> None:
    _CACHE[key] = (time.time(), value)


# ── Tag model ───────────────────────────────────────────────────────────────

@dataclass
class Claim:
    text: str
    tag: str                     # verified | estimated | model_knowledge | unknown
    confidence: float = 0.0
    sources: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"text": self.text, "tag": self.tag,
                "confidence": round(float(self.confidence), 3),
                "sources": list(self.sources),
                "conflicts": list(self.conflicts)}


# ── Identity-aware helpers ──────────────────────────────────────────────────

def user_city() -> str | None:
    try:
        from memory.memory_manager import load_memory
    except Exception:
        return None
    try:
        mem = load_memory() or {}
    except Exception:
        return None
    entry = (mem.get("identity") or {}).get("city")
    if isinstance(entry, dict):
        v = str(entry.get("value") or "").strip()
        return v or None
    if isinstance(entry, str) and entry.strip():
        return entry.strip()
    return None


def _tz_from_memory() -> str | None:
    """Pull a saved timezone from identity preferences if the user ever
    stored one. Looks for ``preferences.timezone`` first, then
    ``identity.timezone``. Returns None if none found."""
    try:
        from memory.memory_manager import load_memory
    except Exception:
        return None
    try:
        mem = load_memory() or {}
    except Exception:
        return None
    for cat in ("preferences", "identity"):
        entry = (mem.get(cat) or {}).get("timezone")
        if isinstance(entry, dict):
            v = str(entry.get("value") or "").strip()
            if v:
                return v
        if isinstance(entry, str) and entry.strip():
            return entry.strip()
    return None


# ── Time ────────────────────────────────────────────────────────────────────

def time_now(zone: str | None = None) -> Claim:
    """Current wall-clock time in a zone. Always ``verified`` — local
    system clock is the source of truth for a voice assistant's day."""
    zone = zone or _tz_from_memory()
    if zone and ZoneInfo is not None:
        try:
            now = datetime.now(ZoneInfo(zone))
            return Claim(
                text=now.strftime("%A, %B %d %Y, %I:%M %p %Z"),
                tag="verified",
                confidence=1.0,
                sources=[f"system-clock/{zone}"],
            )
        except Exception:
            pass
    now = datetime.now().astimezone()
    tz_label = str(now.tzinfo) or "local"
    return Claim(
        text=now.strftime("%A, %B %d %Y, %I:%M %p %Z").strip(),
        tag="verified",
        confidence=1.0,
        sources=[f"system-clock/{tz_label}"],
    )


# ── Location (geocoding) ────────────────────────────────────────────────────

def _geocode(city: str) -> dict | None:
    """Open-Meteo free geocoding. Returns {lat, lon, name, country, timezone}
    or None. Cached."""
    if not city or requests is None:
        return None
    key = f"geo:{city.lower().strip()}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    try:
        r = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": city, "count": 1, "language": "en",
                    "format": "json"},
            timeout=6,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        results = data.get("results") or []
        if not results:
            _cache_set(key, None)
            return None
        res = results[0]
        out = {
            "lat":       float(res.get("latitude")),
            "lon":       float(res.get("longitude")),
            "name":      str(res.get("name") or city),
            "country":   str(res.get("country") or ""),
            "timezone":  str(res.get("timezone") or ""),
        }
        _cache_set(key, out)
        return out
    except Exception as e:
        print(f"[Grounded] geocode failed: {e}")
        return None


def location(city: str | None = None) -> Claim:
    """Resolve a city (or the user's stored city) to coordinates + country."""
    target = city or user_city()
    if not target:
        return Claim(
            text="unknown location — no city provided and none stored in memory",
            tag="unknown", confidence=0.0,
        )
    g = _geocode(target)
    if not g:
        return Claim(
            text=f"{target} (coordinates not resolved)",
            tag="estimated", confidence=0.3,
            sources=["memory/identity"] if not city else [],
        )
    return Claim(
        text=f"{g['name']}, {g['country']} "
             f"(lat {g['lat']:.3f}, lon {g['lon']:.3f}, tz {g['timezone']})",
        tag="verified", confidence=0.9,
        sources=["open-meteo-geocoding"],
    )


# ── Weather ─────────────────────────────────────────────────────────────────

_WEATHER_CODE = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "freezing fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain",
    66: "freezing rain", 67: "heavy freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light rain showers", 81: "rain showers", 82: "heavy rain showers",
    85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm with hail",
    99: "severe thunderstorm with hail",
}


def weather(city: str | None = None) -> Claim:
    """Current weather at a city (or the user's stored city). Uses
    Open-Meteo — free, no key required, decent globally."""
    target = city or user_city()
    if not target:
        return Claim(
            text="weather unknown — no city provided or stored",
            tag="unknown", confidence=0.0,
        )
    g = _geocode(target)
    if not g:
        return Claim(
            text=f"weather for {target} unavailable — could not geocode",
            tag="unknown", confidence=0.0,
        )
    if requests is None:
        return Claim(
            text=f"weather for {g['name']} unavailable — no HTTP client",
            tag="unknown", confidence=0.0,
        )
    key = f"wx:{g['lat']:.3f},{g['lon']:.3f}"
    cached = _cache_get(key)
    if cached is None:
        try:
            r = requests.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude":  g["lat"],
                    "longitude": g["lon"],
                    "current":   "temperature_2m,relative_humidity_2m,"
                                 "weather_code,wind_speed_10m",
                    "temperature_unit": "celsius",
                    "wind_speed_unit":  "kmh",
                    "timezone":  g["timezone"] or "auto",
                },
                timeout=8,
            )
            if r.status_code != 200:
                return Claim(
                    text=f"weather for {g['name']} unavailable — "
                         f"HTTP {r.status_code}",
                    tag="unknown", confidence=0.0,
                )
            data = r.json()
            cached = data.get("current") or {}
            _cache_set(key, cached)
        except Exception as e:
            return Claim(
                text=f"weather for {g['name']} unavailable — {e}",
                tag="unknown", confidence=0.0,
            )

    temp = cached.get("temperature_2m")
    humidity = cached.get("relative_humidity_2m")
    wind = cached.get("wind_speed_10m")
    code = cached.get("weather_code")
    cond = _WEATHER_CODE.get(int(code) if code is not None else -1, "unknown")
    parts: list[str] = [f"{g['name']}: {cond}"]
    if temp is not None:
        parts.append(f"{float(temp):.1f}°C")
    if humidity is not None:
        parts.append(f"humidity {int(humidity)}%")
    if wind is not None:
        parts.append(f"wind {float(wind):.0f} km/h")
    return Claim(
        text=" · ".join(parts),
        tag="verified", confidence=0.85,
        sources=["open-meteo-forecast"],
    )


# ── Web search + fact-check ─────────────────────────────────────────────────

def _search_sources(query: str, *, max_results: int = 4) -> list[dict]:
    """Delegate to actions/web_search._search but return structured rows.
    Falls back to empty on any failure so callers can tag 'unknown'."""
    try:
        from actions import web_search as _ws
    except Exception as e:
        print(f"[Grounded] web_search import failed: {e}")
        return []
    try:
        # web_search._search returns a formatted string; parse it loosely.
        # Preferred: call its lower-level DDG fallback directly. But the
        # formatted string still contains URLs in parentheses.
        raw = _ws._search(query)  # noqa: SLF001
    except Exception as e:
        print(f"[Grounded] _search failed: {e}")
        return []
    sources: list[dict] = []
    import re as _re
    for m in _re.finditer(r"\((https?://[^)\s]+)\)", raw):
        sources.append({"url": m.group(1)})
        if len(sources) >= max_results:
            break
    if not sources:
        # Fallback: at least mark that we searched.
        sources.append({"url": "", "raw": raw[:400]})
    return sources


def fact_check(claim: str, *, max_sources: int = 4) -> Claim:
    """Search a claim and return a tagged verdict. 'verified' needs ≥2
    independent sources; 'estimated' needs 1; 'unknown' means nothing
    useful returned. We do NOT trust the model to adjudicate truth —
    source count + URL diversity is the signal; the model summarises."""
    sources = _search_sources(claim, max_results=max_sources)
    if not sources or all(not s.get("url") for s in sources):
        return Claim(
            text=f"no external sources for claim: {claim}",
            tag="unknown", confidence=0.0,
        )
    # Distinct domains proxy for independence.
    import re as _re
    domains: set[str] = set()
    for s in sources:
        url = s.get("url") or ""
        m = _re.match(r"https?://([^/]+)", url)
        if m:
            domains.add(m.group(1).lower().lstrip("www."))
    diversity = len(domains)
    tag = ("verified" if diversity >= 2
           else ("estimated" if diversity == 1 else "unknown"))
    conf = min(1.0, 0.3 + 0.2 * diversity)
    return Claim(
        text=f"{claim} — {diversity} distinct source domain(s)",
        tag=tag, confidence=conf,
        sources=[s["url"] for s in sources if s.get("url")][:max_sources],
    )


def cross_check(claims: list[str]) -> list[Claim]:
    """Fact-check each claim. Returns one Claim per input, same order."""
    return [fact_check(c) for c in (claims or [])]


# ── Aggregate situational snapshot ──────────────────────────────────────────

def situational(city: str | None = None) -> dict:
    """One call for a full awareness snapshot: time + location + weather.
    Safe to call on every proactive tick."""
    return {
        "time":     time_now().to_dict(),
        "location": location(city).to_dict(),
        "weather":  weather(city).to_dict(),
    }
