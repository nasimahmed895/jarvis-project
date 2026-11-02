"""Interface contracts (typing.Protocol) for the market package.

Nothing here imports a concrete implementation. Implementations declare
themselves conformant by matching the Protocol signatures; no inheritance
required.
"""
from __future__ import annotations

from typing import Any, Protocol, Sequence, runtime_checkable

from .types import OHLCVFrame, Timeframe


@runtime_checkable
class DataProvider(Protocol):
    """Public market-data source. All methods are synchronous."""

    name: str

    def ohlcv(
        self,
        symbol: str,
        timeframe: Timeframe,
        limit: int,
        end_ts_ms: int | None = None,
    ) -> OHLCVFrame: ...

    def ticker(self, symbol: str) -> dict[str, Any]: ...

    def is_symbol_supported(self, symbol: str) -> bool: ...


@runtime_checkable
class Strategy(Protocol):
    """Deterministic strategy producing BUY BIAS / SHORT BIAS / NO TRADE."""

    name: str
    version: str
    supported_timeframes: Sequence[Timeframe]

    def evaluate(self, frame: OHLCVFrame, context: dict[str, Any]) -> dict[str, Any]: ...


@runtime_checkable
class RiskGate(Protocol):
    """Pre-trade gate. Returns an accepted/rejected decision with a reason."""

    def check(self, proposed: dict[str, Any], account_state: dict[str, Any]) -> dict[str, Any]: ...


@runtime_checkable
class Reporter(Protocol):
    """Writes a report to a verified-on-disk path. Returns the final Path."""

    def write(self, payload: dict[str, Any], out_dir: str) -> str: ...
