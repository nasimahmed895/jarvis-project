"""Spoken-response (Bengali) composer for the voice action.

Produces a structured Advisory that the voice action reads back verbatim and
the report layer saves in both JSON and Markdown. The design keeps the
following guarantees:

- The final-decision label matches the one computed by
  `pipeline.compute_final_decision` (always one of BUY BIAS, SHORT BIAS,
  NO TRADE).
- A SHORT BIAS NEVER produces a "sell short" instruction. The project is
  spot-only; the advisor explicitly tells the user to wait or avoid buying.
- Trade levels (entry, stop-loss, target, R/R) are only presented as
  actionable when the final decision is BUY BIAS AND the strategy actually
  produced valid numeric levels. Any missing level falls back to the
  Bengali "প্রযোজ্য নয়" (not applicable yet).
- Nothing is fabricated. Prices are passed through from the strategy or
  omitted; indicator phrases are templated from real values.

The spoken response pattern follows the user's example:
    "স্যার, {asset}-এর {timeframe} analysis শেষ। সিদ্ধান্ত: {label}। ..."
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .forecast.trend_forecast import SpotGuidance, TrendForecast
from .strategies.base import EntryCondition


NA_BN = "প্রযোজ্য নয়"
NA_EN = "Not applicable yet"
NO_ENTRY_BN = "কোনো valid entry এখনো নেই"
NO_ENTRY_EN = "No valid entry yet"


_ASSET_BN = {
    "BTC": "Bitcoin",
    "BTCUSDT": "Bitcoin",
    "ETH": "Ethereum",
    "ETHUSDT": "Ethereum",
    "SOL": "Solana",
    "SOLUSDT": "Solana",
    "BNB": "BNB",
    "BNBUSDT": "BNB",
    "XRP": "XRP",
    "XRPUSDT": "XRP",
    "ADA": "Cardano",
    "ADAUSDT": "Cardano",
    "DOGE": "Dogecoin",
    "DOGEUSDT": "Dogecoin",
}


@dataclass(frozen=True)
class Advisory:
    final_decision: str                # BUY BIAS | SHORT BIAS | NO TRADE
    final_decision_reason: str         # English, from the policy
    asset_display: str                 # Bengali-friendly label (Bitcoin, Ethereum, …)
    timeframe: str                     # the headline timeframe label (e.g. "1h")
    what_to_do_now_bn: str
    entry_display: str                 # Bengali price/condition or NA_BN
    stop_loss_display: str
    target_display: str
    rr_display: str
    reason_bn: str
    next_step_bn: str
    voice_summary_bn: str
    # Everything the report may want to show in full, including SHORT levels
    # that are intentionally NOT read aloud.
    raw_levels: dict[str, Any]
    trend_forecast_bn: str = ""        # concise Bengali summary of the TrendForecast


def _asset_bn(symbol: str) -> str:
    s = (symbol or "").upper()
    for key, name in _ASSET_BN.items():
        if s == key or s.startswith(key):
            return name
    return symbol


def _fmt_price(value: float | None) -> str:
    if value is None:
        return NA_BN
    if value >= 1_000:
        return f"{value:,.2f}"
    if value >= 1:
        return f"{value:.4f}"
    return f"{value:.6f}"


def _fmt_entry_condition(entry: EntryCondition | None) -> str:
    if entry is None:
        return NO_ENTRY_BN
    if entry.kind == "price" and entry.price is not None:
        return _fmt_price(entry.price)
    if entry.kind == "condition" and entry.description:
        return entry.description
    return NO_ENTRY_BN


def _reason_for_no_trade(strategy_decision: str, multi_tf_bias: str) -> str:
    if strategy_decision == "NO TRADE" and multi_tf_bias == "NO TRADE":
        return "Strategy ও multi-timeframe signal দুটোই NO TRADE। trend confirmation সম্পূর্ণ নয়।"
    if strategy_decision == "NO TRADE":
        return "Strategy signal NO TRADE। EMA ও trend confirmation এখনো অসম্পূর্ণ।"
    if multi_tf_bias == "NO TRADE":
        return "Multi-timeframe bias mixed, direction নিশ্চিত নয়।"
    # Direction conflict.
    return (
        f"Strategy signal {strategy_decision} কিন্তু multi-timeframe bias "
        f"{multi_tf_bias} — timeframe conflict আছে।"
    )


def _reason_for_buy(indicators: dict[str, Any]) -> str:
    bits = []
    if "ema_20" in indicators and "ema_50" in indicators:
        bits.append("EMA20 EMA50-এর উপরে")
    if "adx" in indicators:
        bits.append(f"ADX={float(indicators['adx']):.1f}")
    if "rsi" in indicators:
        bits.append(f"RSI={float(indicators['rsi']):.1f}")
    core = ", ".join(bits) if bits else "trend indicator-গুলো buy দিকে"
    return f"{core}। Strategy ও multi-timeframe agreement আছে।"


def _reason_for_short(indicators: dict[str, Any]) -> str:
    bits = []
    if "ema_20" in indicators and "ema_50" in indicators:
        bits.append("EMA20 EMA50-এর নিচে")
    if "adx" in indicators:
        bits.append(f"ADX={float(indicators['adx']):.1f}")
    if "rsi" in indicators:
        bits.append(f"RSI={float(indicators['rsi']):.1f}")
    core = ", ".join(bits) if bits else "trend indicator-গুলো sell দিকে"
    return f"{core}। spot buy এখনই উপযুক্ত নয়।"


def _next_step(final_decision: str) -> str:
    if final_decision == "BUY BIAS":
        return "Entry condition পূরণ হলে spot buy consider করতে পারেন। Stop-loss অবশ্যই set করবেন।"
    if final_decision == "SHORT BIAS":
        return "Trend reverse হয়ে EMA20 আবার buy দিকে গেলে এবং multi-timeframe agreement এলে research আবার করব।"
    # NO TRADE
    return "EMA ও trend confirmation-এর শর্ত পূরণ হলে বা clear breakout এলে আবার research করব।"


_STATE_BN = {
    "UPTREND": "UPTREND",
    "DOWNTREND": "DOWNTREND",
    "RANGING": "RANGING",
    "UNCERTAIN": "UNCERTAIN",
}

_GUIDANCE_BN = {
    SpotGuidance.WAIT: "WAIT — এখনই নতুন position নেবেন না।",
    SpotGuidance.CONSIDER_BUY_SETUP: "CONSIDER BUY SETUP — strategy ও trend aligned, levels available।",
    SpotGuidance.HOLD_REVIEW: "HOLD REVIEW — existing position থাকলে stop review করুন।",
    SpotGuidance.CONSIDER_SELLING_OR_REDUCING_RISK: "CONSIDER SELLING / REDUCING RISK — existing holders exposure কমানোর কথা ভাবতে পারেন।",
}


def _render_trend_forecast_bn(tf: TrendForecast | None) -> str:
    if tf is None:
        return ""
    missing = (
        f" (missing TFs: {', '.join(tf.missing_timeframes)})" if tf.missing_timeframes else ""
    )
    short_part = (
        f"short-term ({', '.join(tf.short_term.timeframes_included)}): {tf.short_term.state.value}"
        if tf.short_term else "short-term: data unavailable"
    )
    medium_part = (
        f"medium-term ({', '.join(tf.medium_term.timeframes_included)}): {tf.medium_term.state.value}"
        if tf.medium_term else "medium-term: data unavailable"
    )
    guidance_bn = _GUIDANCE_BN.get(tf.spot_guidance, tf.spot_guidance.value)
    return (
        f"Trend state: {tf.current_state.value}{missing}। "
        f"{short_part}; {medium_part}। "
        f"Horizon hint: {tf.horizon_hint}। "
        f"Spot guidance: {guidance_bn} "
        f"Holding assessment: {tf.hold_assessment.recommendation} "
        f"New-entry assessment: {tf.new_entry_assessment.recommendation} "
        f"Horizon is a scenario, time-এর guarantee নয়। "
        f"Confidence (heuristic): {tf.heuristic_confidence}।"
    )


def build_advisory(
    *,
    final_decision: str,
    final_decision_reason: str,
    strategy_decision: str,
    multi_tf_bias: str,
    asset: str,
    timeframe: str,
    reference_price: float,
    entry: EntryCondition | None,
    stop_loss: float | None,
    targets: tuple[float, ...],
    rr_estimate: float | None,
    indicators: dict[str, Any],
    trend_forecast: TrendForecast | None = None,
) -> Advisory:
    asset_display = _asset_bn(asset)
    headline = f"স্যার, {asset_display}-এর {timeframe} analysis শেষ। সিদ্ধান্ত: {final_decision}।"

    # Default to "not applicable" everywhere. We fill real values only on the
    # BUY BIAS actionable branch and only when the strategy produced them.
    entry_display = NO_ENTRY_BN
    stop_display = NA_BN
    target_display = NA_BN
    rr_display = NA_BN

    if final_decision == "BUY BIAS":
        has_entry = entry is not None
        has_stop = stop_loss is not None and stop_loss > 0
        has_target = bool(targets) and targets[0] is not None
        if has_entry and has_stop and has_target:
            entry_display = _fmt_entry_condition(entry)
            stop_display = _fmt_price(stop_loss)
            target_display = _fmt_price(targets[0])
            rr_display = f"{rr_estimate:.2f}" if rr_estimate else NA_BN
        else:
            # Final said BUY BIAS but strategy did not produce complete levels.
            # Fall back to "not applicable" so no fabricated numbers leak.
            entry_display = NO_ENTRY_BN
        action_bn = (
            "Trend confirmation হলে spot buy consider করতে পারেন। "
            "অবশ্যই stop-loss রাখবেন, risk সীমিত রাখবেন।"
        )
        reason_bn = _reason_for_buy(indicators)
    elif final_decision == "SHORT BIAS":
        # Spot-only project: never emit a "short this" instruction. The
        # strategy's short-side levels still exist in `raw_levels` for the
        # report, but they are NOT read out as actionable trade levels.
        action_bn = (
            "এখনই নতুন buy করবেন না। Trend sell দিকে, spot buy এখন উপযুক্ত নয়। "
            "Reversal signal-এর জন্য অপেক্ষা করুন।"
        )
        reason_bn = _reason_for_short(indicators)
    else:
        action_bn = (
            "এখনই নতুন trade entry করবেন না। Signal পরিষ্কার হওয়া পর্যন্ত অপেক্ষা করুন।"
        )
        reason_bn = _reason_for_no_trade(strategy_decision, multi_tf_bias)

    next_step_bn = _next_step(final_decision)
    trend_forecast_bn = _render_trend_forecast_bn(trend_forecast)

    voice_summary_bn = (
        f"{headline} {action_bn} "
        f"Entry: {entry_display}। Stop-loss: {stop_display}। "
        f"Target: {target_display}। Risk-reward: {rr_display}। "
        f"কারণ: {reason_bn} পরবর্তী ধাপ: {next_step_bn}"
    )
    if trend_forecast_bn:
        voice_summary_bn += " Trend forecast: " + trend_forecast_bn

    raw_levels = {
        "entry": _entry_to_dict(entry),
        "stop_loss": stop_loss,
        "targets": list(targets),
        "rr_estimate": rr_estimate,
        "indicators": dict(indicators),
    }

    return Advisory(
        final_decision=final_decision,
        final_decision_reason=final_decision_reason,
        asset_display=asset_display,
        timeframe=timeframe,
        what_to_do_now_bn=action_bn,
        entry_display=entry_display,
        stop_loss_display=stop_display,
        target_display=target_display,
        rr_display=rr_display,
        reason_bn=reason_bn,
        next_step_bn=next_step_bn,
        voice_summary_bn=voice_summary_bn,
        raw_levels=raw_levels,
        trend_forecast_bn=trend_forecast_bn,
    )


def _entry_to_dict(entry: EntryCondition | None) -> dict | None:
    if entry is None:
        return None
    return {"kind": entry.kind, "price": entry.price, "description": entry.description}
