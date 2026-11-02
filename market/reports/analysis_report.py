"""Market-analysis report composer.

Builds a payload from a strategy decision + multi-timeframe context + data
quality notes, writes Markdown and JSON side by side, returns both paths.

Report-integrity contract:
    * `conclusion`, `final_decision`, and the Markdown headline all show the
      SAME label (the policy decision from the pipeline).
    * `strategy_decision` and `multi_tf_bias` are preserved as separate
      fields so the user can see why the final label is what it is.
    * A zone whose midpoint is further than `_FAR_ZONE_PCT` from the
      reference price is labelled "far" with its percentage distance; a
      missing zone states an explicit reason instead of silent `n/a`.
"""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..analysis.multi_tf import TimeframeReport
from ..analysis.context import ContextResult
from ..strategies.base import StrategyDecision
from ..types import OHLCVFrame
from .paths import report_paths
from .writer import write_json, write_markdown


# Zones whose midpoint is further than this fraction from the reference
# price are flagged. Tuned for intraday crypto; swing TFs may want wider.
_FAR_ZONE_PCT = 0.05


def _advisory_payload(advisory: Any) -> dict[str, Any] | None:
    if advisory is None:
        return None
    return {
        "final_decision": advisory.final_decision,
        "final_decision_reason": advisory.final_decision_reason,
        "asset_display": advisory.asset_display,
        "timeframe": advisory.timeframe,
        "what_to_do_now_bn": advisory.what_to_do_now_bn,
        "entry_display": advisory.entry_display,
        "stop_loss_display": advisory.stop_loss_display,
        "target_display": advisory.target_display,
        "rr_display": advisory.rr_display,
        "reason_bn": advisory.reason_bn,
        "next_step_bn": advisory.next_step_bn,
        "voice_summary_bn": advisory.voice_summary_bn,
        "raw_levels": advisory.raw_levels,
        "trend_forecast_bn": advisory.trend_forecast_bn,
    }


def _trend_forecast_payload(tf: Any) -> dict[str, Any] | None:
    if tf is None:
        return None
    return {
        "current_state": tf.current_state.value,
        "short_term": None if tf.short_term is None else {
            "timeframes": list(tf.short_term.timeframes_included),
            "state": tf.short_term.state.value,
            "reason": tf.short_term.reason,
        },
        "medium_term": None if tf.medium_term is None else {
            "timeframes": list(tf.medium_term.timeframes_included),
            "state": tf.medium_term.state.value,
            "reason": tf.medium_term.reason,
        },
        "horizon_hint": tf.horizon_hint,
        "horizon_disclaimer": tf.horizon_disclaimer,
        "scenarios": [
            {
                "name": s.name,
                "conditions_for": list(s.conditions_for),
                "invalidation": list(s.invalidation),
            }
            for s in tf.scenarios
        ],
        "spot_guidance": tf.spot_guidance.value,
        "spot_guidance_reason": tf.spot_guidance_reason,
        "hold_assessment": {
            "audience": tf.hold_assessment.audience,
            "recommendation": tf.hold_assessment.recommendation,
            "detail": tf.hold_assessment.detail,
            "invalidation": tf.hold_assessment.invalidation,
        },
        "new_entry_assessment": {
            "audience": tf.new_entry_assessment.audience,
            "recommendation": tf.new_entry_assessment.recommendation,
            "detail": tf.new_entry_assessment.detail,
            "invalidation": tf.new_entry_assessment.invalidation,
        },
        "heuristic_confidence": tf.heuristic_confidence,
        "confidence_note": tf.confidence_note,
        "missing_timeframes": list(tf.missing_timeframes),
    }


def _zone_payload(zone, ref_price: float, kind: str) -> dict[str, Any]:
    """Explicit zone descriptor. Never silently returns `n/a` for missing."""
    if zone is None:
        return {
            "available": False,
            "reason": (
                f"no clustered {kind} zone identified (need >=2 confirmed swings "
                f"within 0.5% of each other below/above the reference price)"
            ),
            "distance_pct": None,
            "range": None,
            "touches": 0,
        }
    mid = (zone.low + zone.high) / 2.0
    dist_pct = (mid - ref_price) / ref_price if ref_price else 0.0
    note = ""
    if abs(dist_pct) > _FAR_ZONE_PCT:
        note = f"far ({dist_pct:+.2%} from reference)"
    else:
        note = f"within {_FAR_ZONE_PCT:.0%} ({dist_pct:+.2%})"
    return {
        "available": True,
        "reason": "",
        "distance_pct": dist_pct,
        "range": [zone.low, zone.high],
        "touches": zone.touches,
        "note": note,
    }


def _fmt_zone_md(zp: dict[str, Any]) -> str:
    if not zp["available"]:
        return f"not available — {zp['reason']}"
    lo, hi = zp["range"]
    return f"{lo:.4f} – {hi:.4f} ({zp['note']}, touches={zp['touches']})"


def _render_markdown(payload: dict[str, Any]) -> str:
    d = payload
    lines = [
        f"# Market Analysis — {d['asset']} ({d['timeframe']})",
        "",
        f"**Report ID:** `{d['report_id']}`  ",
        f"**Generated at (UTC ms):** {d['generated_at_ms']}  ",
        f"**Data source:** {d['source']}  ",
        f"**Last candle time (ms):** {d['last_candle_time_ms']}  ",
        f"**Reference price:** {d['reference_price']}  ",
        "",
        "## Final Decision",
        "",
        f"**{d['final_decision']}**  ",
        f"_Reason_: {d['final_decision_reason']}  ",
        "",
        f"- Strategy alone said: `{d['strategy_decision']}`  ",
        f"- Multi-timeframe bias: `{d['multi_tf_bias']}`  ",
        f"- Heuristic confidence (NOT a probability): {d['heuristic_confidence']}  ",
        "",
        "## Action Guidance (research, not a live signal)",
        "",
    ]
    adv = d.get("advisory") or {}
    if adv:
        lines += [
            f"- **What to do now**: {adv.get('what_to_do_now_bn','')}  ",
            f"- **Entry**: {adv.get('entry_display','')}  ",
            f"- **Stop-loss**: {adv.get('stop_loss_display','')}  ",
            f"- **Target**: {adv.get('target_display','')}  ",
            f"- **Risk/Reward**: {adv.get('rr_display','')}  ",
            f"- **Reason (bn)**: {adv.get('reason_bn','')}  ",
            f"- **Next step (bn)**: {adv.get('next_step_bn','')}  ",
            "",
            "### Spoken Bengali summary",
            "",
            f"> {adv.get('voice_summary_bn','')}",
            "",
        ]
    lines += [
        "## Strategy",
        "",
        f"- Name: `{d['strategy_name']}`  ",
        f"- Version: `{d['strategy_version']}`  ",
        f"- Stop-loss: {d['stop_loss']}  ",
        f"- Target(s): {', '.join(str(t) for t in d['targets']) if d['targets'] else 'n/a'}  ",
        f"- Risk/Reward estimate: {d['rr_estimate']}  ",
        f"- Entry: {d['entry']}  ",
        "",
        "## Evidence",
        "",
    ]
    lines += [f"- {e}" for e in d["evidence"]] or ["- (none)"]
    lines += ["", "## Conflicts / Caveats", ""]
    lines += [f"- {c}" for c in d["conflicts"]] or ["- (none)"]
    tf_block = d.get("trend_forecast") or {}
    if tf_block:
        lines += ["", "## Trend Forecast and Spot Position Guidance", ""]
        lines += [
            f"- **Current market state**: {tf_block['current_state']}  ",
        ]
        if tf_block.get("short_term"):
            s = tf_block["short_term"]
            lines.append(
                f"- **Short-term** ({', '.join(s['timeframes'])}): {s['state']}  "
            )
        else:
            lines.append("- **Short-term**: data unavailable  ")
        if tf_block.get("medium_term"):
            m = tf_block["medium_term"]
            lines.append(
                f"- **Medium-term** ({', '.join(m['timeframes'])}): {m['state']}  "
            )
        else:
            lines.append("- **Medium-term**: data unavailable  ")
        if tf_block.get("missing_timeframes"):
            lines.append(
                f"- **Missing timeframes**: {', '.join(tf_block['missing_timeframes'])}  "
            )
        lines += [
            f"- **Horizon hint**: {tf_block['horizon_hint']}  ",
            f"- **Horizon disclaimer**: {tf_block['horizon_disclaimer']}  ",
            f"- **Spot guidance**: {tf_block['spot_guidance']}  ",
            f"- **Spot guidance reason**: {tf_block['spot_guidance_reason']}  ",
            f"- **Heuristic confidence**: {tf_block['heuristic_confidence']} ({tf_block['confidence_note']})  ",
            "",
            "### Scenarios",
            "",
        ]
        for sc in tf_block["scenarios"]:
            lines.append(f"- **{sc['name'].capitalize()}**  ")
            lines.append("    Conditions:  ")
            for cond in sc["conditions_for"]:
                lines.append(f"    - {cond}  ")
            lines.append("    Invalidation:  ")
            for inv in sc["invalidation"]:
                lines.append(f"    - {inv}  ")
        lines += ["", "### Hold-position assessment (for users already holding)", ""]
        ha = tf_block["hold_assessment"]
        lines += [
            f"- **Recommendation**: {ha['recommendation']}  ",
            f"- **Detail**: {ha['detail']}  ",
            f"- **Invalidation**: {ha['invalidation']}  ",
            "",
            "### New-entry assessment (for users not yet holding)",
            "",
        ]
        ne = tf_block["new_entry_assessment"]
        lines += [
            f"- **Recommendation**: {ne['recommendation']}  ",
            f"- **Detail**: {ne['detail']}  ",
            f"- **Invalidation**: {ne['invalidation']}  ",
        ]
    lines += ["", "## Multi-Timeframe View", ""]
    for row in d["multi_tf"]:
        lines.append(
            f"- **{row['timeframe']}**: regime={row['regime']}  "
        )
        lines.append(f"    - support: {_fmt_zone_md(row['nearest_support'])}  ")
        lines.append(f"    - resistance: {_fmt_zone_md(row['nearest_resistance'])}  ")
    lines += ["", "## Market Context", ""]
    for ctx in d["context"]:
        if ctx["available"]:
            tf = ctx["value"].get("timeframe") if isinstance(ctx["value"], dict) else None
            label = f"**{ctx['name']}**"
            if tf:
                label += f" (timeframe={tf})"
            lines.append(f"- {label}: {ctx['value']}")
        else:
            lines.append(f"- **{ctx['name']}**: not available — {ctx['reason']}")
    lines += [
        "",
        "## Data Quality",
        "",
        f"- bars received: {d['data_quality']['received']}  ",
        f"- staleness seconds: {d['data_quality']['stale_age_seconds']}  ",
        f"- warnings: {', '.join(d['data_quality']['warnings']) or 'none'}  ",
        "",
        "## Limitations",
        "",
        "- No forecast is a guarantee. Past results do not imply future results.",
        "- Spot-market analysis only. Not applicable without adjustment to leveraged futures.",
        "- News / sentiment NOT integrated in v1.",
        "- Backtests include fees and slippage estimates; real execution may differ.",
    ]
    return "\n".join(lines) + "\n"


def write_analysis_report(
    *,
    out_dir: Path,
    frame: OHLCVFrame,
    decision: StrategyDecision,
    per_tf_reports: list[TimeframeReport],
    context_results: list[ContextResult],
    data_quality: dict[str, Any],
    bias: dict[str, Any],
    final_decision: str,
    final_decision_reason: str,
    advisory: Any = None,
    trend_forecast: Any = None,
) -> tuple[Path, Path, str]:
    md_path, json_path, report_id = report_paths(out_dir, frame.symbol, kind="analysis")
    ref_price = decision.reference_price
    advisory_payload = _advisory_payload(advisory)
    trend_forecast_payload = _trend_forecast_payload(trend_forecast)
    payload: dict[str, Any] = {
        "report_id": report_id,
        "generated_at_ms": frame.retrieved_at_ms,
        "asset": frame.symbol,
        "timeframe": frame.timeframe.value,
        "source": frame.source,
        "last_candle_time_ms": frame.candles[-1].close_time_ms if frame.candles else 0,
        "reference_price": ref_price,
        # Report-integrity triplet: these three fields are always consistent
        # with each other and with ResearchResult.conclusion.
        "final_decision": final_decision,
        "final_decision_reason": final_decision_reason,
        "conclusion": final_decision,          # kept for backwards compatibility
        "strategy_decision": decision.decision,
        "multi_tf_bias": bias["bias"],
        "advisory": advisory_payload,
        "trend_forecast": trend_forecast_payload,
        "heuristic_confidence": decision.heuristic_confidence,
        "strategy_name": decision.strategy_name,
        "strategy_version": decision.strategy_version,
        "stop_loss": decision.stop_loss,
        "targets": list(decision.targets),
        "rr_estimate": decision.rr_estimate,
        "entry": asdict(decision.entry) if decision.entry else None,
        "evidence": list(decision.evidence),
        "conflicts": list(decision.conflicts),
        "cancellation_conditions": list(decision.cancellation_conditions),
        "indicators": decision.extras,
        "multi_tf": [
            {
                "timeframe": r.timeframe,
                "regime": r.regime.label,
                "nearest_support": _zone_payload(r.nearest_support, ref_price, "support"),
                "nearest_resistance": _zone_payload(r.nearest_resistance, ref_price, "resistance"),
            }
            for r in per_tf_reports
        ],
        "bias": bias,
        "context": [
            {
                "name": c.name,
                "available": c.available,
                "value": c.value,
                "reason": c.reason,
            }
            for c in context_results
        ],
        "data_quality": data_quality,
    }
    write_json(json_path, payload)
    write_markdown(md_path, _render_markdown(payload))
    return md_path, json_path, report_id
