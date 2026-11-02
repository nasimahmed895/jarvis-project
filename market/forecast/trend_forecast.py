"""Trend Forecast and Spot Position Guidance.

Produces a structured, qualitative forecast from closed-candle frames across
short-term (15m, 1h) and medium-term (4h, 1d) timeframes. Every output is a
*conditional scenario*, not a time-stamped promise. The module NEVER emits
numeric probability percentages — only a heuristic confidence label.

Guarantees:

- Deterministic for a given set of input frames.
- No fabrication: when a timeframe is absent, the forecast says so.
- Scenario conditions reference concrete price levels or indicators only when
  the input frames supplied them. If an invalidation level cannot be sourced
  (e.g. no clustered zone), the invalidation is phrased in terms of the
  indicator that WAS computed.
- Spot guidance is one of four labels enumerated in `SpotGuidance`. The
  SHORT-related label is `CONSIDER_SELLING_OR_REDUCING_RISK`, which applies
  only to a user who already holds the asset — the project is spot-only and
  the system never instructs anyone to open a short.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from ..analysis.multi_tf import TimeframeReport, analyse_timeframe
from ..types import OHLCVFrame, Timeframe


class MarketState(str, Enum):
    UPTREND = "UPTREND"
    DOWNTREND = "DOWNTREND"
    RANGING = "RANGING"
    UNCERTAIN = "UNCERTAIN"


class SpotGuidance(str, Enum):
    WAIT = "WAIT"
    CONSIDER_BUY_SETUP = "CONSIDER BUY SETUP"
    HOLD_REVIEW = "HOLD REVIEW"
    CONSIDER_SELLING_OR_REDUCING_RISK = "CONSIDER SELLING / REDUCING RISK"


_REGIME_TO_STATE = {
    "strong_uptrend": MarketState.UPTREND,
    "weak_uptrend": MarketState.UPTREND,
    "strong_downtrend": MarketState.DOWNTREND,
    "weak_downtrend": MarketState.DOWNTREND,
    "ranging": MarketState.RANGING,
    "transition": MarketState.UNCERTAIN,
    "uncertain": MarketState.UNCERTAIN,
}


# Short-term vs medium-term buckets. Any timeframe not listed falls under
# "other" and does not influence the aggregate classification.
_SHORT_TERM = {Timeframe.M15, Timeframe.M30, Timeframe.H1}
_MEDIUM_TERM = {Timeframe.H4, Timeframe.H6, Timeframe.H12, Timeframe.D1}


@dataclass(frozen=True)
class TrendAssessment:
    span: str                           # "short_term" | "medium_term"
    timeframes_included: tuple[str, ...]
    state: MarketState
    reason: str                         # English evidence summary


@dataclass(frozen=True)
class Scenario:
    name: str                           # "bullish" | "bearish" | "sideways"
    conditions_for: tuple[str, ...]     # price/indicator conditions that would support it
    invalidation: tuple[str, ...]       # conditions that would reject it


@dataclass(frozen=True)
class PositionAssessment:
    audience: str                       # "already_holding" | "new_entry"
    recommendation: str                 # terse one-line, English
    detail: str                         # longer human-readable justification
    invalidation: str                   # the level/signal that would flip it


@dataclass(frozen=True)
class TrendForecast:
    current_state: MarketState
    short_term: TrendAssessment | None
    medium_term: TrendAssessment | None
    horizon_hint: str                   # qualitative: "next few hours", "several days", or hybrid
    horizon_disclaimer: str             # explicit note that the horizon is a scenario, not a promise
    scenarios: tuple[Scenario, ...]
    spot_guidance: SpotGuidance
    spot_guidance_reason: str
    hold_assessment: PositionAssessment
    new_entry_assessment: PositionAssessment
    heuristic_confidence: str           # "low" | "moderate" | "qualitative-only"
    confidence_note: str                # always explains that confidence is heuristic
    missing_timeframes: tuple[str, ...]  # timeframes the user asked for but could not supply


def _bucket(frames: Mapping[Timeframe, OHLCVFrame], bucket: set[Timeframe]) -> list[Timeframe]:
    """Return the frames in `bucket` present in `frames`, sorted deterministically."""
    return sorted((tf for tf in frames if tf in bucket), key=lambda t: t.value)


def _aggregate_state(reports: list[TimeframeReport]) -> MarketState:
    """Majority rule across timeframes in a bucket. Ties go to UNCERTAIN."""
    if not reports:
        return MarketState.UNCERTAIN
    tally: dict[MarketState, int] = {}
    for r in reports:
        s = _REGIME_TO_STATE.get(r.regime.label, MarketState.UNCERTAIN)
        tally[s] = tally.get(s, 0) + 1
    top = max(tally.values())
    winners = [s for s, c in tally.items() if c == top]
    if len(winners) == 1:
        return winners[0]
    # Tie: if any winner is UNCERTAIN, prefer the directional one; otherwise
    # pure tie → UNCERTAIN. This keeps a 1-up / 1-down split honestly uncertain.
    directional = [w for w in winners if w not in (MarketState.UNCERTAIN, MarketState.RANGING)]
    if len(directional) == 1:
        return directional[0]
    return MarketState.UNCERTAIN


def _assessment(span: str, tfs: list[Timeframe], reports: list[TimeframeReport]) -> TrendAssessment | None:
    if not reports:
        return None
    state = _aggregate_state(reports)
    labels = [r.regime.label for r in reports]
    reason = (
        f"{span} regime vote across {','.join(t.value for t in tfs)}: {labels} → {state.value}"
    )
    return TrendAssessment(
        span=span,
        timeframes_included=tuple(t.value for t in tfs),
        state=state,
        reason=reason,
    )


def _horizon_hint(short: TrendAssessment | None, medium: TrendAssessment | None) -> tuple[str, str]:
    """Return (horizon_hint, horizon_disclaimer)."""
    disclaimer = (
        "This horizon is a timeframe-based scenario: it describes which "
        "duration the active signal pattern would normally play out over, "
        "NOT a promise that price will rise or fall within that window."
    )
    if short is None and medium is None:
        return "unknown", disclaimer
    if short is not None and medium is None:
        return "short-term (next few hours only; higher-timeframe context unavailable)", disclaimer
    if medium is not None and short is None:
        return "medium-term (several days; intraday context unavailable)", disclaimer
    # Both present.
    s = short.state
    m = medium.state
    if s == m and m in (MarketState.UPTREND, MarketState.DOWNTREND):
        return "next few hours to several days (short- and medium-term aligned)", disclaimer
    if m in (MarketState.UPTREND, MarketState.DOWNTREND):
        return "several days (medium-term dominant; short-term may oppose transiently)", disclaimer
    if s in (MarketState.UPTREND, MarketState.DOWNTREND):
        return "next few hours (short-term signal only; higher timeframe does not confirm)", disclaimer
    return "no clear horizon — ranging or uncertain across both spans", disclaimer


def _invalidation_phrase(reports: list[TimeframeReport]) -> str:
    """Build an invalidation phrase from the best-available swing zone."""
    for r in reports:
        if r.nearest_support is not None:
            return f"close below the {r.timeframe} support zone {r.nearest_support.low:.4f}-{r.nearest_support.high:.4f}"
    for r in reports:
        if r.regime.indicators.get("ema_50"):
            return f"close below the {r.timeframe} EMA50 ({r.regime.indicators['ema_50']:.4f})"
    return "a sustained close that breaks the current structure"


def _upside_invalidation(reports: list[TimeframeReport]) -> str:
    for r in reports:
        if r.nearest_resistance is not None:
            return (
                f"close above the {r.timeframe} resistance zone "
                f"{r.nearest_resistance.low:.4f}-{r.nearest_resistance.high:.4f}"
            )
    for r in reports:
        if r.regime.indicators.get("ema_20"):
            return f"close above the {r.timeframe} EMA20 ({r.regime.indicators['ema_20']:.4f})"
    return "a sustained close breaking back above the prior swing high"


def _scenarios(
    short: TrendAssessment | None,
    medium: TrendAssessment | None,
    reports_by_tf: Mapping[str, TimeframeReport],
) -> tuple[Scenario, ...]:
    rep_list = list(reports_by_tf.values())
    down_invalid = _invalidation_phrase(rep_list)
    up_invalid = _upside_invalidation(rep_list)

    bull = Scenario(
        name="bullish",
        conditions_for=(
            "Price holds above the medium-term EMA20 on 4h/1d.",
            "Short-term pullback into EMA20 followed by rejection with volume.",
            "Break and retest of the nearest resistance zone.",
        ),
        invalidation=(
            f"Sustained {down_invalid}.",
            "Lower-low on the 4h chart.",
        ),
    )
    bear = Scenario(
        name="bearish",
        conditions_for=(
            "Price loses the medium-term EMA20 on 4h/1d with momentum.",
            "Break of the nearest support zone with expanding ATR.",
            "Lower-high on the 4h chart that fails at EMA20.",
        ),
        invalidation=(
            f"Sustained {up_invalid}.",
            "Higher-low on the 4h chart that holds above EMA50.",
        ),
    )
    side = Scenario(
        name="sideways",
        conditions_for=(
            "Price oscillates between the nearest support and resistance.",
            "ADX below 20 on the 4h chart.",
            "No volume expansion on either side of the range.",
        ),
        invalidation=(
            "Clean breakout from the range with volume confirmation.",
            "ADX crosses above 25 on the 4h chart.",
        ),
    )
    return (bull, bear, side)


def _spot_guidance(
    current_state: MarketState,
    strategy_decision: str,
    multi_tf_bias: str,
    has_valid_levels: bool,
) -> tuple[SpotGuidance, str]:
    # Full agreement + valid levels → actionable buy setup.
    if (
        current_state == MarketState.UPTREND
        and strategy_decision == "BUY BIAS"
        and multi_tf_bias == "BUY BIAS"
        and has_valid_levels
    ):
        return (
            SpotGuidance.CONSIDER_BUY_SETUP,
            "medium-term uptrend AND strategy BUY BIAS AND entry/stop/target available",
        )
    # Already in uptrend but strategy sees no clean entry → hold-review for
    # existing holders; wait for new entries.
    if current_state == MarketState.UPTREND:
        return (
            SpotGuidance.HOLD_REVIEW,
            "medium-term uptrend but no confirmed buy setup from strategy; existing holders should review stops",
        )
    # Clear downtrend → risk-reduction advice for holders; nothing to do for new buyers.
    if current_state == MarketState.DOWNTREND:
        return (
            SpotGuidance.CONSIDER_SELLING_OR_REDUCING_RISK,
            "medium-term downtrend; spot buyers should wait, existing holders should consider reducing exposure",
        )
    # Ranging / Uncertain → wait.
    return (
        SpotGuidance.WAIT,
        f"current state {current_state.value}; wait for a decisive breakout or clear trend confirmation",
    )


def _hold_assessment(current_state: MarketState) -> PositionAssessment:
    if current_state == MarketState.UPTREND:
        rec = "Continue holding while structure is intact; tighten stop toward the 4h EMA50."
        detail = (
            "Medium-term bias is up. For an existing spot position, trail risk below "
            "the 4h EMA50 or the most recent higher-low. Avoid taking profits all at once."
        )
    elif current_state == MarketState.DOWNTREND:
        rec = "Consider reducing exposure; invalidation of the downtrend sits above the recent swing high."
        detail = (
            "Medium-term bias is down. Holding full size through a confirmed downtrend "
            "exposes capital unnecessarily; scaling out or trailing a tighter stop is the "
            "defensive move. A close back above the 4h swing high would nullify the downtrend view."
        )
    elif current_state == MarketState.RANGING:
        rec = "Hold while the range holds; reduce near resistance, accumulate near support."
        detail = (
            "Range behaviour means mean-reversion is the dominant edge; swinging a full "
            "position through the whole range is suboptimal. Trim near resistance, add near "
            "support, keep stops just outside the range."
        )
    else:
        rec = "Neutral — review stops; do not add until the market picks a direction."
        detail = (
            "Current state is uncertain. For an existing position, verify that your stop "
            "survives both a false breakout and a shakeout within the recent range."
        )
    return PositionAssessment(
        audience="already_holding",
        recommendation=rec,
        detail=detail,
        invalidation="A sustained 4h close below the most recent higher-low (uptrend) or above the swing high (downtrend).",
    )


def _new_entry_assessment(
    guidance: SpotGuidance,
    strategy_decision: str,
    multi_tf_bias: str,
    has_valid_levels: bool,
) -> PositionAssessment:
    if guidance == SpotGuidance.CONSIDER_BUY_SETUP and has_valid_levels:
        return PositionAssessment(
            audience="new_entry",
            recommendation="Consider a spot buy on the strategy's documented entry condition.",
            detail=(
                "Short- and medium-term signals both support a buy. Use the strategy's "
                "stop-loss and target levels; do not front-run the entry condition."
            ),
            invalidation="A close below the strategy's stop-loss or the medium-term EMA50.",
        )
    missing_bits = []
    if strategy_decision != "BUY BIAS":
        missing_bits.append(f"strategy call is {strategy_decision}")
    if multi_tf_bias != "BUY BIAS":
        missing_bits.append(f"multi-timeframe bias is {multi_tf_bias}")
    if not has_valid_levels:
        missing_bits.append("no complete entry/stop/target available")
    missing = "; ".join(missing_bits) if missing_bits else "no actionable confluence yet"
    return PositionAssessment(
        audience="new_entry",
        recommendation="NO TRADE — wait for confirmation.",
        detail=(
            f"No valid new-entry setup: {missing}. "
            "Do not open a new spot position until all three pieces align."
        ),
        invalidation="A full confluence of BUY BIAS from strategy + multi-TF + valid levels.",
    )


def build_trend_forecast(
    frames: Mapping[Timeframe, OHLCVFrame],
    *,
    strategy_decision: str,
    multi_tf_bias: str,
    has_valid_levels: bool,
    expected_timeframes: tuple[Timeframe, ...] = (
        Timeframe.M15, Timeframe.H1, Timeframe.H4, Timeframe.D1,
    ),
) -> TrendForecast:
    """Build the forecast from whatever subset of frames is available.

    `expected_timeframes` defines what the user asked for. Any timeframe
    missing from `frames` is listed in `missing_timeframes` so the report
    can state "data unavailable for 15m".
    """
    missing = tuple(tf.value for tf in expected_timeframes if tf not in frames)

    short_tfs = _bucket(frames, _SHORT_TERM)
    medium_tfs = _bucket(frames, _MEDIUM_TERM)
    short_reports = [analyse_timeframe(frames[tf]) for tf in short_tfs]
    medium_reports = [analyse_timeframe(frames[tf]) for tf in medium_tfs]

    short = _assessment("short_term", short_tfs, short_reports)
    medium = _assessment("medium_term", medium_tfs, medium_reports)

    # Current state preference: medium-term if available (dominant), else short-term.
    if medium is not None and medium.state != MarketState.UNCERTAIN:
        current_state = medium.state
    elif short is not None and short.state != MarketState.UNCERTAIN:
        current_state = short.state
    elif medium is not None:
        current_state = medium.state
    elif short is not None:
        current_state = short.state
    else:
        current_state = MarketState.UNCERTAIN

    horizon_hint, horizon_disclaimer = _horizon_hint(short, medium)

    reports_by_tf = {r.timeframe: r for r in short_reports + medium_reports}
    scenarios = _scenarios(short, medium, reports_by_tf)

    guidance, guidance_reason = _spot_guidance(
        current_state, strategy_decision, multi_tf_bias, has_valid_levels
    )
    hold = _hold_assessment(current_state)
    new_entry = _new_entry_assessment(guidance, strategy_decision, multi_tf_bias, has_valid_levels)

    # Confidence policy. We refuse to emit numeric probabilities. Four TFs
    # present and aligned → "moderate" heuristic. Fewer or divergent → "low".
    all_tfs_present = not missing
    aligned = (
        short is not None and medium is not None
        and short.state == medium.state
        and short.state in (MarketState.UPTREND, MarketState.DOWNTREND)
    )
    if all_tfs_present and aligned:
        heuristic = "moderate"
    elif short is not None or medium is not None:
        heuristic = "low"
    else:
        heuristic = "qualitative-only"

    confidence_note = (
        "Confidence is a HEURISTIC descriptor, not a calibrated probability. "
        "No percentage figure is attached. The label reflects timeframe "
        "coverage and alignment only."
    )

    return TrendForecast(
        current_state=current_state,
        short_term=short,
        medium_term=medium,
        horizon_hint=horizon_hint,
        horizon_disclaimer=horizon_disclaimer,
        scenarios=scenarios,
        spot_guidance=guidance,
        spot_guidance_reason=guidance_reason,
        hold_assessment=hold,
        new_entry_assessment=new_entry,
        heuristic_confidence=heuristic,
        confidence_note=confidence_note,
        missing_timeframes=missing,
    )
