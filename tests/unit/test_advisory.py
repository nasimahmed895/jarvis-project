"""Advisory composer tests — spoken Bengali response for every branch.

Verifies the user-facing rules:
    * BUY BIAS → trade levels present AND spoken Bengali says "buy".
    * SHORT BIAS → spot-only project MUST NOT instruct a short; entry/stop/
      target all read as "প্রযোজ্য নয়".
    * NO TRADE → every level reads as "প্রযোজ্য নয়".
    * BUY BIAS with incomplete levels (strategy returned None) → treated as
      no valid entry; no fabricated numbers appear.
    * The spoken summary always includes the asset name and timeframe.
    * Nothing in SHORT BIAS output tells the user to sell / short.
"""
from __future__ import annotations

from market.advisory import NA_BN, NO_ENTRY_BN, build_advisory
from market.strategies.base import EntryCondition


def _common(**overrides):
    base = dict(
        final_decision="NO TRADE",
        final_decision_reason="test",
        strategy_decision="NO TRADE",
        multi_tf_bias="NO TRADE",
        asset="BTCUSDT",
        timeframe="1h",
        reference_price=20_000.0,
        entry=None,
        stop_loss=None,
        targets=(),
        rr_estimate=None,
        indicators={"ema_20": 1.0, "ema_50": 1.0, "adx": 25.0, "rsi": 50.0},
    )
    base.update(overrides)
    return base


def test_buy_bias_with_full_levels_shows_prices() -> None:
    adv = build_advisory(**_common(
        final_decision="BUY BIAS",
        final_decision_reason="agree",
        strategy_decision="BUY BIAS",
        multi_tf_bias="BUY BIAS",
        entry=EntryCondition(kind="condition", description="pullback to EMA20"),
        stop_loss=19_600.0,
        targets=(20_800.0,),
        rr_estimate=1.67,
    ))
    assert adv.final_decision == "BUY BIAS"
    assert "buy" in adv.what_to_do_now_bn.lower() or "spot" in adv.what_to_do_now_bn
    assert adv.entry_display == "pullback to EMA20"
    assert "19,600" in adv.stop_loss_display
    assert "20,800" in adv.target_display
    assert adv.rr_display == "1.67"
    # Spoken summary MUST include asset + timeframe + the label.
    assert "Bitcoin" in adv.voice_summary_bn
    assert "1h" in adv.voice_summary_bn
    assert "BUY BIAS" in adv.voice_summary_bn


def test_buy_bias_with_missing_levels_shows_na() -> None:
    """Strategy said BUY BIAS but produced no stop/target → no fabricated levels."""
    adv = build_advisory(**_common(
        final_decision="BUY BIAS",
        final_decision_reason="agree",
        strategy_decision="BUY BIAS",
        multi_tf_bias="BUY BIAS",
        entry=None,
        stop_loss=None,
        targets=(),
        rr_estimate=None,
    ))
    assert adv.entry_display == NO_ENTRY_BN
    assert adv.stop_loss_display == NA_BN
    assert adv.target_display == NA_BN
    assert adv.rr_display == NA_BN


def test_short_bias_never_instructs_short_sell() -> None:
    adv = build_advisory(**_common(
        final_decision="SHORT BIAS",
        final_decision_reason="agree",
        strategy_decision="SHORT BIAS",
        multi_tf_bias="SHORT BIAS",
        entry=EntryCondition(kind="condition", description="rally to EMA20"),
        stop_loss=20_400.0,
        targets=(19_200.0,),
        rr_estimate=1.67,
    ))
    # Spot-only guard: SHORT BIAS output must NEVER tell the user to short/sell.
    bad_tokens = ["short-sell", "sell short", "short করুন", "sell korun", "sell করুন"]
    joined = (adv.what_to_do_now_bn + " " + adv.voice_summary_bn).lower()
    for bad in bad_tokens:
        assert bad.lower() not in joined, f"found forbidden instruction: {bad!r}"
    # And: entry/stop/target MUST read as 'not applicable yet'.
    assert adv.entry_display == NO_ENTRY_BN
    assert adv.stop_loss_display == NA_BN
    assert adv.target_display == NA_BN
    assert adv.rr_display == NA_BN
    # Raw levels still carry the strategy numbers so the saved report keeps
    # the information even though the spoken response does not.
    assert adv.raw_levels["stop_loss"] == 20_400.0


def test_no_trade_everything_na() -> None:
    adv = build_advisory(**_common())
    assert adv.final_decision == "NO TRADE"
    assert adv.entry_display == NO_ENTRY_BN
    assert adv.stop_loss_display == NA_BN
    assert adv.target_display == NA_BN
    assert adv.rr_display == NA_BN
    assert "নতুন trade entry করবেন না" in adv.what_to_do_now_bn or "NO TRADE" in adv.voice_summary_bn


def test_conflicting_timeframes_reason_mentions_both_signals() -> None:
    adv = build_advisory(**_common(
        final_decision="NO TRADE",
        final_decision_reason="direction conflict: strategy=BUY BIAS, multi-TF=SHORT BIAS",
        strategy_decision="BUY BIAS",
        multi_tf_bias="SHORT BIAS",
    ))
    assert "BUY BIAS" in adv.reason_bn or "conflict" in adv.reason_bn.lower()
    assert "SHORT BIAS" in adv.reason_bn or "conflict" in adv.reason_bn.lower()
    assert adv.entry_display == NO_ENTRY_BN


def test_voice_summary_contains_action_sections() -> None:
    adv = build_advisory(**_common(
        final_decision="BUY BIAS",
        final_decision_reason="agree",
        strategy_decision="BUY BIAS",
        multi_tf_bias="BUY BIAS",
        entry=EntryCondition(kind="condition", description="pullback to EMA20"),
        stop_loss=19_600.0,
        targets=(20_800.0,),
        rr_estimate=1.67,
    ))
    for section in ("Entry:", "Stop-loss:", "Target:", "Risk-reward:", "কারণ:", "পরবর্তী ধাপ:"):
        assert section in adv.voice_summary_bn, f"missing section: {section}"


def test_next_step_tailored_per_decision() -> None:
    for label in ("BUY BIAS", "SHORT BIAS", "NO TRADE"):
        adv = build_advisory(**_common(final_decision=label))
        assert adv.next_step_bn
        if label == "BUY BIAS":
            assert "entry" in adv.next_step_bn.lower() or "spot buy" in adv.next_step_bn.lower()
        if label == "NO TRADE":
            assert "research" in adv.next_step_bn.lower() or "আবার" in adv.next_step_bn
