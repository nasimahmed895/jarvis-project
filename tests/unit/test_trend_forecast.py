"""Trend Forecast + Spot Position Guidance unit tests."""
from __future__ import annotations

import pytest

from market.forecast.trend_forecast import (
    MarketState,
    SpotGuidance,
    build_trend_forecast,
)
from market.types import Timeframe
from tests.fixtures.synth import constant_frame, trending_frame


def _four_frames(up: bool = True):
    """Return a dict of 15m/1h/4h/1d frames all trending the same way."""
    step = 0.1 if up else -0.1
    start = 100.0 if up else 300.0
    return {
        Timeframe.M15: trending_frame(300, Timeframe.M15, start_price=start, step_price=step),
        Timeframe.H1:  trending_frame(300, Timeframe.H1, start_price=start, step_price=step),
        Timeframe.H4:  trending_frame(300, Timeframe.H4, start_price=start, step_price=step),
        Timeframe.D1:  trending_frame(300, Timeframe.D1, start_price=start, step_price=step),
    }


def test_aligned_uptrend_state_and_horizon() -> None:
    f = _four_frames(up=True)
    tf = build_trend_forecast(
        f, strategy_decision="BUY BIAS", multi_tf_bias="BUY BIAS", has_valid_levels=True
    )
    assert tf.current_state == MarketState.UPTREND
    assert tf.short_term is not None and tf.short_term.state == MarketState.UPTREND
    assert tf.medium_term is not None and tf.medium_term.state == MarketState.UPTREND
    assert "next few hours to several days" in tf.horizon_hint
    assert tf.spot_guidance == SpotGuidance.CONSIDER_BUY_SETUP
    # Scenarios present and ordered.
    assert tuple(s.name for s in tf.scenarios) == ("bullish", "bearish", "sideways")
    # Hold assessment for an uptrend recommends holding.
    assert "Continue holding" in tf.hold_assessment.recommendation
    # New-entry assessment is actionable.
    assert tf.new_entry_assessment.recommendation.startswith("Consider a spot buy")
    assert tf.heuristic_confidence == "moderate"
    assert tf.missing_timeframes == ()


def test_aligned_downtrend_state_and_sell_guidance() -> None:
    f = _four_frames(up=False)
    tf = build_trend_forecast(
        f, strategy_decision="SHORT BIAS", multi_tf_bias="SHORT BIAS", has_valid_levels=False
    )
    assert tf.current_state == MarketState.DOWNTREND
    assert tf.spot_guidance == SpotGuidance.CONSIDER_SELLING_OR_REDUCING_RISK
    # New-entry: NO TRADE for spot buyers.
    assert tf.new_entry_assessment.recommendation.startswith("NO TRADE")
    assert "reduce" in tf.hold_assessment.recommendation.lower() or "reducing" in tf.hold_assessment.recommendation.lower()


def test_flat_market_is_ranging_or_uncertain_and_waits() -> None:
    f = {
        Timeframe.M15: constant_frame(300, Timeframe.M15),
        Timeframe.H1:  constant_frame(300, Timeframe.H1),
        Timeframe.H4:  constant_frame(300, Timeframe.H4),
        Timeframe.D1:  constant_frame(300, Timeframe.D1),
    }
    tf = build_trend_forecast(
        f, strategy_decision="NO TRADE", multi_tf_bias="NO TRADE", has_valid_levels=False
    )
    assert tf.current_state in (MarketState.RANGING, MarketState.UNCERTAIN)
    assert tf.spot_guidance == SpotGuidance.WAIT
    assert tf.new_entry_assessment.recommendation.startswith("NO TRADE")


def test_missing_timeframes_recorded_honestly() -> None:
    # Only 1h and 4h supplied — 15m and 1d missing.
    f = {
        Timeframe.H1: trending_frame(300, Timeframe.H1, start_price=100.0, step_price=0.1),
        Timeframe.H4: trending_frame(300, Timeframe.H4, start_price=100.0, step_price=0.1),
    }
    tf = build_trend_forecast(
        f, strategy_decision="BUY BIAS", multi_tf_bias="BUY BIAS", has_valid_levels=True,
        expected_timeframes=(Timeframe.M15, Timeframe.H1, Timeframe.H4, Timeframe.D1),
    )
    assert "15m" in tf.missing_timeframes
    assert "1d" in tf.missing_timeframes
    assert tf.short_term is not None
    assert tf.medium_term is not None
    # Confidence drops to "low" when not all four TFs are present.
    assert tf.heuristic_confidence == "low"


def test_conflict_downgrades_horizon_and_guidance() -> None:
    # Short-term up, medium-term down → conflicting state.
    f = {
        Timeframe.M15: trending_frame(300, Timeframe.M15, start_price=100.0, step_price=0.1),
        Timeframe.H1:  trending_frame(300, Timeframe.H1, start_price=100.0, step_price=0.1),
        Timeframe.H4:  trending_frame(300, Timeframe.H4, start_price=300.0, step_price=-0.1),
        Timeframe.D1:  trending_frame(300, Timeframe.D1, start_price=300.0, step_price=-0.1),
    }
    tf = build_trend_forecast(
        f, strategy_decision="NO TRADE", multi_tf_bias="NO TRADE", has_valid_levels=False
    )
    # Medium-term dominates: current state should be DOWN.
    assert tf.current_state == MarketState.DOWNTREND
    # Short-term is UP, so they conflict → confidence should NOT be moderate.
    assert tf.heuristic_confidence != "moderate"
    assert tf.spot_guidance == SpotGuidance.CONSIDER_SELLING_OR_REDUCING_RISK


def test_horizon_disclaimer_present_always() -> None:
    f = _four_frames(up=True)
    tf = build_trend_forecast(
        f, strategy_decision="BUY BIAS", multi_tf_bias="BUY BIAS", has_valid_levels=True
    )
    assert "scenario" in tf.horizon_disclaimer.lower()
    assert "promise" in tf.horizon_disclaimer.lower()
    assert "NOT" in tf.horizon_disclaimer or "not" in tf.horizon_disclaimer


def test_no_numeric_probability_in_confidence() -> None:
    f = _four_frames(up=True)
    tf = build_trend_forecast(
        f, strategy_decision="BUY BIAS", multi_tf_bias="BUY BIAS", has_valid_levels=True
    )
    assert "%" not in tf.heuristic_confidence
    assert "%" not in tf.confidence_note
    for digit in "0123456789":
        assert digit not in tf.heuristic_confidence


def test_scenarios_contain_invalidation_phrases() -> None:
    f = _four_frames(up=True)
    tf = build_trend_forecast(
        f, strategy_decision="BUY BIAS", multi_tf_bias="BUY BIAS", has_valid_levels=True
    )
    for sc in tf.scenarios:
        assert sc.conditions_for and all(sc.conditions_for)
        assert sc.invalidation and all(sc.invalidation)


def test_hold_and_new_entry_are_separate() -> None:
    f = _four_frames(up=True)
    tf = build_trend_forecast(
        f, strategy_decision="BUY BIAS", multi_tf_bias="BUY BIAS", has_valid_levels=True
    )
    assert tf.hold_assessment.audience == "already_holding"
    assert tf.new_entry_assessment.audience == "new_entry"


def test_no_timeframes_yields_uncertain() -> None:
    tf = build_trend_forecast(
        {}, strategy_decision="NO TRADE", multi_tf_bias="NO TRADE", has_valid_levels=False
    )
    assert tf.current_state == MarketState.UNCERTAIN
    assert tf.short_term is None and tf.medium_term is None
    assert tf.heuristic_confidence == "qualitative-only"


def test_spot_guidance_requires_valid_levels_for_buy_setup() -> None:
    # Everything bullish but strategy produced no levels → HOLD_REVIEW, not BUY SETUP.
    f = _four_frames(up=True)
    tf = build_trend_forecast(
        f, strategy_decision="BUY BIAS", multi_tf_bias="BUY BIAS", has_valid_levels=False
    )
    assert tf.spot_guidance == SpotGuidance.HOLD_REVIEW
    assert "NO TRADE" in tf.new_entry_assessment.recommendation
