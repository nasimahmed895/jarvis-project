"""Core immutable types used throughout the market package.

Pure stdlib — no pandas, no numpy at this layer. Indicator modules may convert
to numpy arrays on demand but the canonical representation here is a
dataclass sequence of candles so tests and persistence stay simple.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Sequence


class Timeframe(str, Enum):
    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    M30 = "30m"
    H1 = "1h"
    H2 = "2h"
    H4 = "4h"
    H6 = "6h"
    H12 = "12h"
    D1 = "1d"
    D3 = "3d"
    W1 = "1w"

    @classmethod
    def from_str(cls, value: str) -> "Timeframe":
        s = str(value).strip().lower()
        for tf in cls:
            if tf.value == s:
                return tf
        raise ValueError(f"Unsupported timeframe: {value!r}")


# How many milliseconds in each supported timeframe. Used by the staleness
# check and the backtest clock. Values are exchange-independent.
TIMEFRAME_MS: dict[Timeframe, int] = {
    Timeframe.M1: 60_000,
    Timeframe.M5: 5 * 60_000,
    Timeframe.M15: 15 * 60_000,
    Timeframe.M30: 30 * 60_000,
    Timeframe.H1: 60 * 60_000,
    Timeframe.H2: 2 * 60 * 60_000,
    Timeframe.H4: 4 * 60 * 60_000,
    Timeframe.H6: 6 * 60 * 60_000,
    Timeframe.H12: 12 * 60 * 60_000,
    Timeframe.D1: 24 * 60 * 60_000,
    Timeframe.D3: 3 * 24 * 60 * 60_000,
    Timeframe.W1: 7 * 24 * 60 * 60_000,
}


@dataclass(frozen=True, slots=True)
class Candle:
    """One OHLCV bar. `is_closed` must be False for an in-progress candle."""

    open_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time_ms: int
    is_closed: bool = True

    def __post_init__(self) -> None:
        if self.close_time_ms <= self.open_time_ms:
            raise ValueError("close_time_ms must be greater than open_time_ms")
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("high is less than another OHLC component")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("low is greater than another OHLC component")
        if self.volume < 0:
            raise ValueError("volume must be non-negative")


@dataclass(frozen=True, slots=True)
class OHLCVFrame:
    """A validated sequence of candles for one (symbol, timeframe) pair.

    `source` names the provider (e.g. 'binance_public'). `retrieved_at_ms` is
    the wall-clock moment the data was pulled — not the latest candle's time.
    """

    symbol: str
    timeframe: Timeframe
    source: str
    retrieved_at_ms: int
    candles: Sequence[Candle] = field(default_factory=tuple)

    def __len__(self) -> int:
        return len(self.candles)

    @property
    def latest(self) -> Candle | None:
        return self.candles[-1] if self.candles else None

    @property
    def latest_closed(self) -> Candle | None:
        for c in reversed(self.candles):
            if c.is_closed:
                return c
        return None

    def closes(self) -> tuple[float, ...]:
        return tuple(c.close for c in self.candles)

    def highs(self) -> tuple[float, ...]:
        return tuple(c.high for c in self.candles)

    def lows(self) -> tuple[float, ...]:
        return tuple(c.low for c in self.candles)

    def volumes(self) -> tuple[float, ...]:
        return tuple(c.volume for c in self.candles)
