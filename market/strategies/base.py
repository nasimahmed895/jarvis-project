"""Strategy base class + decision records.

A strategy evaluates a validated OHLCVFrame and returns a `StrategyDecision`.
`decision` is one of LONG_BIAS / SHORT_BIAS / NO_TRADE. Every decision must
carry its reasoning in `evidence` and `conflicts`. Entry prices are advisory
when the strategy cannot pin a specific level — in that case the strategy
returns a condition-based entry instead.

A strategy is identified by (name, version). Rules for a given version MUST
be immutable — if the rules change, bump the version so historical results
remain reproducible against the exact rules that produced them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from ..types import OHLCVFrame, Timeframe


# Public label used in reports, spoken response, and the ResearchResult. The
# constant name stays `LONG_BIAS` for internal clarity (it IS a long bias on
# spot); the VALUE is "BUY BIAS" because that is the user-facing wording.
LONG_BIAS = "BUY BIAS"
BUY_BIAS = LONG_BIAS   # explicit alias for callers that read nicer this way
SHORT_BIAS = "SHORT BIAS"
NO_TRADE = "NO TRADE"
VALID_DECISIONS = (LONG_BIAS, SHORT_BIAS, NO_TRADE)


@dataclass(frozen=True, slots=True)
class EntryCondition:
    """Either an exact price or a condition expressed as a human-readable
    sentence plus an optional target band."""

    kind: str                # "price" | "condition"
    price: float | None = None
    description: str = ""


@dataclass(frozen=True, slots=True)
class StrategyDecision:
    strategy_name: str
    strategy_version: str
    asset: str
    timeframe: str
    source: str
    analysis_ts_ms: int
    reference_price: float
    decision: str
    entry: EntryCondition | None = None
    stop_loss: float | None = None
    targets: tuple[float, ...] = field(default_factory=tuple)
    rr_estimate: float | None = None
    evidence: tuple[str, ...] = field(default_factory=tuple)
    conflicts: tuple[str, ...] = field(default_factory=tuple)
    cancellation_conditions: tuple[str, ...] = field(default_factory=tuple)
    heuristic_confidence: float = 0.0   # NOT a probability.
    extras: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.decision not in VALID_DECISIONS:
            raise ValueError(f"decision must be one of {VALID_DECISIONS}")
        if not self.strategy_name or not self.strategy_version:
            raise ValueError("strategy_name and strategy_version are required")


class Strategy:
    """Base class for all strategies.

    Subclasses declare:
        name                        unique strategy name
        version                     bump when rules change
        supported_timeframes        tuple of Timeframe
        min_bars                    minimum number of candles to evaluate

    and implement `evaluate(frame, context) -> StrategyDecision`.
    """

    name: str = ""
    version: str = ""
    supported_timeframes: Sequence[Timeframe] = ()
    min_bars: int = 1

    def supports(self, tf: Timeframe) -> bool:
        return tf in self.supported_timeframes

    def evaluate(self, frame: OHLCVFrame, context: dict[str, Any]) -> StrategyDecision:
        raise NotImplementedError
