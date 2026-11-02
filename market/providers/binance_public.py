"""Binance public spot-market provider.

Only unauthenticated endpoints are called. No API key is required and no
authenticated functionality is implemented. The provider is read-only.

Endpoints used:
    GET /api/v3/klines           historical OHLCV
    GET /api/v3/ticker/24hr      24-hour ticker summary
    GET /api/v3/ticker/price     latest price only
    GET /api/v3/exchangeInfo     tradable symbols

A fixed-rate token-bucket caps outbound requests; Binance publishes a
request-weight limit of 1200 per minute per IP. We stay well below it.

Retries use exponential backoff capped at three attempts for transient
failures (HTTP 5xx, timeouts). HTTP 429 (rate limit) and 418 (banned) are
raised immediately as `RateLimitError` with no automatic retry — the caller
decides how to escalate.
"""
from __future__ import annotations

import threading
import time
from typing import Any

import requests

from ..exceptions import DataUnavailable, ProviderError, RateLimitError
from ..types import Candle, OHLCVFrame, Timeframe


_BASE_URL = "https://api.binance.com"

# Pseudo-weight budget per minute. The real public limit is 1200; we cap much
# lower so no one spot-trading agent can crowd a shared endpoint.
_WEIGHT_PER_MINUTE = 300
_WINDOW_SECONDS = 60.0

# HTTP settings.
_CONNECT_TIMEOUT_S = 5.0
_READ_TIMEOUT_S = 10.0
_MAX_RETRIES = 3
_BACKOFF_BASE_S = 0.75


class _TokenBucket:
    """Simple fixed-window bucket, thread-safe."""

    def __init__(self, limit: int, window_seconds: float):
        self._limit = limit
        self._window = window_seconds
        self._lock = threading.Lock()
        self._window_start = time.monotonic()
        self._used = 0

    def consume(self, weight: int) -> None:
        with self._lock:
            now = time.monotonic()
            if now - self._window_start >= self._window:
                self._window_start = now
                self._used = 0
            if self._used + weight > self._limit:
                sleep_for = self._window - (now - self._window_start)
                time.sleep(max(0.0, sleep_for))
                self._window_start = time.monotonic()
                self._used = 0
            self._used += weight


# All timeframes supported by Binance klines, mapped from our enum. Any
# timeframe added to `Timeframe` must gain an entry here before the provider
# will accept it.
_INTERVAL_MAP: dict[Timeframe, str] = {
    Timeframe.M1: "1m",
    Timeframe.M5: "5m",
    Timeframe.M15: "15m",
    Timeframe.M30: "30m",
    Timeframe.H1: "1h",
    Timeframe.H2: "2h",
    Timeframe.H4: "4h",
    Timeframe.H6: "6h",
    Timeframe.H12: "12h",
    Timeframe.D1: "1d",
    Timeframe.D3: "3d",
    Timeframe.W1: "1w",
}


class BinancePublicProvider:
    name = "binance_public"

    def __init__(
        self,
        base_url: str = _BASE_URL,
        quote_currency: str = "USDT",
        session: requests.Session | None = None,
    ):
        self._base = base_url.rstrip("/")
        self._quote = quote_currency.upper()
        self._session = session or requests.Session()
        self._bucket = _TokenBucket(_WEIGHT_PER_MINUTE, _WINDOW_SECONDS)
        self._symbols_cache: set[str] | None = None

    # ── internal ─────────────────────────────────────────────────────────
    def _symbol_pair(self, asset: str) -> str:
        asset = asset.strip().upper()
        if asset.endswith(self._quote):
            return asset
        return asset + self._quote

    def _get(self, path: str, params: dict[str, Any] | None = None, weight: int = 1) -> Any:
        url = f"{self._base}{path}"
        last_err: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            self._bucket.consume(weight)
            try:
                resp = self._session.get(
                    url,
                    params=params or {},
                    timeout=(_CONNECT_TIMEOUT_S, _READ_TIMEOUT_S),
                    headers={"Accept": "application/json"},
                )
            except requests.RequestException as e:
                last_err = e
                time.sleep(_BACKOFF_BASE_S * (2**attempt))
                continue

            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError as e:
                    raise ProviderError(f"malformed JSON from {path}: {e}") from None
            if resp.status_code in (418, 429):
                raise RateLimitError(
                    f"rate limited by provider on {path} (HTTP {resp.status_code})"
                )
            if 500 <= resp.status_code < 600:
                last_err = ProviderError(f"HTTP {resp.status_code} from {path}")
                time.sleep(_BACKOFF_BASE_S * (2**attempt))
                continue
            # 4xx other than 429/418 is caller-fixable; do not retry.
            raise ProviderError(
                f"HTTP {resp.status_code} from {path}: {resp.text[:200]!r}"
            )
        raise DataUnavailable(f"all retries exhausted for {path}: {last_err}")

    # ── public API ───────────────────────────────────────────────────────
    def is_symbol_supported(self, symbol: str) -> bool:
        if self._symbols_cache is None:
            data = self._get("/api/v3/exchangeInfo", weight=20)
            self._symbols_cache = {
                s["symbol"]
                for s in data.get("symbols", [])
                if s.get("status") == "TRADING" and s.get("isSpotTradingAllowed")
            }
        return self._symbol_pair(symbol) in self._symbols_cache

    def ticker(self, symbol: str) -> dict[str, Any]:
        pair = self._symbol_pair(symbol)
        price = self._get("/api/v3/ticker/price", {"symbol": pair}, weight=1)
        stats = self._get("/api/v3/ticker/24hr", {"symbol": pair}, weight=2)
        return {
            "symbol": pair,
            "price": float(price["price"]),
            "price_change_pct_24h": float(stats.get("priceChangePercent", 0.0)),
            "high_24h": float(stats.get("highPrice", 0.0)),
            "low_24h": float(stats.get("lowPrice", 0.0)),
            "volume_24h": float(stats.get("volume", 0.0)),
            "quote_volume_24h": float(stats.get("quoteVolume", 0.0)),
            "retrieved_at_ms": int(time.time() * 1000),
            "source": self.name,
        }

    def ohlcv(
        self,
        symbol: str,
        timeframe: Timeframe,
        limit: int,
        end_ts_ms: int | None = None,
    ) -> OHLCVFrame:
        if timeframe not in _INTERVAL_MAP:
            raise ProviderError(f"timeframe {timeframe!r} not supported by binance public")
        if not 1 <= limit <= 1000:
            raise ProviderError("binance klines limit must be in [1, 1000]")

        pair = self._symbol_pair(symbol)
        params: dict[str, Any] = {
            "symbol": pair,
            "interval": _INTERVAL_MAP[timeframe],
            "limit": int(limit),
        }
        if end_ts_ms is not None:
            params["endTime"] = int(end_ts_ms)
        rows = self._get("/api/v3/klines", params, weight=2)
        if not isinstance(rows, list):
            raise ProviderError("binance klines response is not a list")

        candles = tuple(_row_to_candle(r) for r in rows)
        return OHLCVFrame(
            symbol=pair,
            timeframe=timeframe,
            source=self.name,
            retrieved_at_ms=int(time.time() * 1000),
            candles=candles,
        )


def _row_to_candle(row: list) -> Candle:
    """Convert one kline row to a `Candle`.

    Binance kline rows have 12 fields; we read the first 11 and infer
    `is_closed` from the close_time being in the past.
    """
    if len(row) < 7:
        raise ProviderError(f"kline row too short: {row[:3]!r}")
    open_ms = int(row[0])
    o = float(row[1])
    h = float(row[2])
    l = float(row[3])
    c = float(row[4])
    v = float(row[5])
    close_ms = int(row[6])
    now_ms = int(time.time() * 1000)
    is_closed = close_ms <= now_ms
    return Candle(
        open_time_ms=open_ms,
        open=o,
        high=h,
        low=l,
        close=c,
        volume=v,
        close_time_ms=close_ms,
        is_closed=is_closed,
    )
