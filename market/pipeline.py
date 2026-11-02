"""End-to-end research pipeline — the glue called by the voice action.

Fetches validated OHLCV for an asset across intraday + swing timeframes,
runs the per-TF analysis, runs the baseline strategy on each, computes a
multi-TF bias, records a forecast if any direction fires, and writes a
Markdown + JSON report. Returns a terse summary dict the voice layer can
read out and the detailed report path.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .advisory import Advisory, build_advisory
from .analysis.context import (
    btc_trend_snapshot,
    correlation_with_btc,
    news_sentiment,
)
from .analysis.multi_tf import analyse_timeframe, multi_tf_bias
from .config import MarketConfig, load_config
from .data.validator import closed_candles_only, validate_frame
from .exceptions import DataUnavailable, MarketError, StaleData, ValidationError
from .forecast.tracker import ForecastTracker
from .forecast.trend_forecast import TrendForecast, build_trend_forecast
from .interfaces import DataProvider
from .logging_setup import get_logger
from .providers.binance_public import BinancePublicProvider
from .reports.analysis_report import write_analysis_report
from .reports.location import resolve_report_dir
from .strategies.registry import get as get_strategy
from .strategies import ma_trend_v1  # noqa: F401  registration side-effect
from .types import OHLCVFrame, Timeframe, TIMEFRAME_MS


# ── Final-decision policy ──────────────────────────────────────────────────
# The spoken summary and the on-disk report always surface the SAME label.
# Policy (documented so a reviewer can audit it in one place):
#
#   * If the strategy returns NO TRADE → final is NO TRADE.
#   * If the multi-TF bias is NO TRADE → final is NO TRADE.
#   * If the two signals disagree on direction → final is NO TRADE.
#   * Otherwise → final is the agreed direction.
#
# This is deliberately conservative: an actionable direction requires both the
# strategy and the multi-TF regime to point the same way. The individual
# signals are preserved as separate fields (`strategy_decision`,
# `multi_tf_bias`) so the user can see why the final decision is what it is.

_DIRECTIONAL = {"BUY BIAS", "SHORT BIAS"}


def compute_final_decision(strategy_decision: str, multi_tf_bias: str) -> tuple[str, str]:
    """Return (final_decision, reason). Pure function, no side effects."""
    if strategy_decision == "NO TRADE":
        return "NO TRADE", "strategy produced NO TRADE"
    if multi_tf_bias == "NO TRADE":
        return "NO TRADE", f"multi-timeframe bias is NO TRADE (strategy said {strategy_decision})"
    if strategy_decision != multi_tf_bias:
        return (
            "NO TRADE",
            f"direction conflict: strategy={strategy_decision}, multi-TF={multi_tf_bias}",
        )
    return strategy_decision, f"strategy and multi-TF both agree on {strategy_decision}"


def timeframe_staleness_threshold(tf: Timeframe, floor_seconds: int) -> int:
    """Timeframe-aware freshness threshold.

    A 1-day candle legitimately sits ~24h since its own close, so a fixed
    120-second threshold would reject every daily analysis. The policy here
    is `max(configured_floor, 1.5 * timeframe_duration)`:

    - 1m → max(floor, 90s)
    - 1h → max(floor, 5400s)
    - 1d → max(floor, ~36h)

    The 1.5× factor leaves slack for a one-candle fetch delay without
    accepting data that has entirely skipped a bar. Candle *closure* time
    is the reference, not retrieval time — this is enforced by
    `validate_frame` which compares `now_ms - latest_closed.close_time_ms`.
    """
    step_s = TIMEFRAME_MS[tf] // 1000
    return max(int(floor_seconds), int(1.5 * step_s))


@dataclass(frozen=True)
class ResearchResult:
    asset: str
    conclusion: str                     # BUY BIAS | SHORT BIAS | NO TRADE | DATA UNAVAILABLE
    strategy_decision: str
    multi_tf_bias: str
    final_decision_reason: str
    heuristic_confidence: float         # NOT a probability
    headline_timeframe: str
    reference_price: float
    report_markdown_path: str
    report_json_path: str
    report_id: str
    data_quality_summary: str
    warnings: tuple[str, ...]
    # Spoken Bengali response. The action handler reads `voice_summary_bn`
    # back to the user; the structured fields (entry_display, stop_loss_display,
    # target_display, rr_display, next_step_bn) are also saved into the
    # report so the on-disk file matches what was spoken.
    advisory: Advisory | None = None


def _build_provider(cfg: MarketConfig) -> DataProvider:
    if cfg.provider == "binance_public":
        return BinancePublicProvider(quote_currency=cfg.quote_currency)
    raise MarketError(f"unknown provider: {cfg.provider}")


def _fetch_frame(
    provider: DataProvider,
    asset: str,
    tf: Timeframe,
    cfg: MarketConfig,
    now_ms: int,
) -> tuple[OHLCVFrame | None, list[str]]:
    warnings: list[str] = []
    try:
        raw = provider.ohlcv(asset, tf, limit=min(cfg.lookback_candles, 1000))
    except Exception as e:  # noqa: BLE001
        warnings.append(f"{tf.value} fetch failed: {type(e).__name__}: {e}")
        return None, warnings
    threshold = timeframe_staleness_threshold(tf, cfg.data_staleness_seconds)
    try:
        clean, q = validate_frame(raw, now_ms, threshold)
    except StaleData as e:
        warnings.append(f"{tf.value} stale (threshold={threshold}s): {e}")
        return None, warnings
    except ValidationError as e:
        warnings.append(f"{tf.value} invalid: {e}")
        return None, warnings
    warnings.extend(q.warnings)
    return closed_candles_only(clean), warnings


def research_asset(
    asset: str,
    *,
    horizon: str = "both",         # "intraday" | "swing" | "both"
    config: MarketConfig | None = None,
    provider: DataProvider | None = None,
    config_path: Path | None = None,
) -> ResearchResult:
    """Run the full research pipeline for `asset` and return a summary."""
    cfg = config or load_config(config_path)
    log = get_logger("pipeline", level=cfg.log_level)
    log.info("research start: asset=%s horizon=%s", asset, horizon)

    if provider is None:
        provider = _build_provider(cfg)

    try:
        if not provider.is_symbol_supported(asset):
            return _data_unavailable_result(
                asset,
                reason="symbol not supported by provider",
                detail=f"{asset} is not a supported symbol on {cfg.provider}",
                warnings=(f"symbol {asset} unsupported",),
            )
    except Exception as e:  # noqa: BLE001
        return _data_unavailable_result(
            asset,
            reason=f"symbol check failed: {type(e).__name__}",
            detail=f"symbol check failed: {type(e).__name__}: {e}",
            warnings=(str(e),),
        )

    tfs: list[Timeframe] = []
    if horizon in ("intraday", "both"):
        tfs.extend(cfg.intraday_timeframes)
    if horizon in ("swing", "both"):
        tfs.extend(cfg.swing_timeframes)
    # Dedupe preserving order.
    seen = set()
    tfs = [tf for tf in tfs if not (tf in seen or seen.add(tf))]

    now_ms = int(time.time() * 1000)
    frames: dict[Timeframe, OHLCVFrame] = {}
    warnings: list[str] = []
    for tf in tfs:
        frame, w = _fetch_frame(provider, asset, tf, cfg, now_ms)
        warnings.extend(w)
        if frame is not None:
            frames[tf] = frame

    if not frames:
        return _data_unavailable_result(
            asset,
            reason="no timeframe produced usable data",
            detail="no timeframe produced usable data",
            warnings=tuple(warnings),
        )

    # Pick the "headline" timeframe for the strategy decision: prefer the
    # longest configured timeframe that returned data (gives the strategy
    # more context).
    sorted_tfs = sorted(frames.keys(), key=lambda t: _tf_rank(t), reverse=True)
    headline_tf = sorted_tfs[0]
    headline_frame = frames[headline_tf]

    strategy = get_strategy("ma_trend_momentum")
    decision = strategy.evaluate(headline_frame, context={})

    per_tf_reports = [analyse_timeframe(frames[tf]) for tf in sorted_tfs]
    bias = multi_tf_bias(per_tf_reports)

    context_results = [
        btc_trend_snapshot(provider),
        correlation_with_btc(provider, asset) if asset.upper() not in ("BTC", "BTCUSDT") else _na_context(),
        news_sentiment(asset),
    ]

    data_quality = {
        "received": len(headline_frame),
        "stale_age_seconds": 0.0,
        "warnings": warnings,
    }

    # Apply the documented final-decision policy BEFORE writing anything.
    # The same label must appear in: ResearchResult.conclusion, the JSON
    # "final_decision" + "conclusion" keys, and the Markdown headline.
    final_decision, final_reason = compute_final_decision(decision.decision, bias["bias"])

    # Build the Trend Forecast + Spot Position Guidance block. Uses the
    # frames we already fetched; honestly reports which of the four
    # expected timeframes (15m, 1h, 4h, 1d) are missing.
    has_valid_levels = (
        decision.entry is not None
        and decision.stop_loss is not None
        and decision.stop_loss > 0
        and bool(decision.targets)
    )
    trend_forecast = build_trend_forecast(
        frames,
        strategy_decision=decision.decision,
        multi_tf_bias=bias["bias"],
        has_valid_levels=has_valid_levels,
    )

    advisory = build_advisory(
        final_decision=final_decision,
        final_decision_reason=final_reason,
        strategy_decision=decision.decision,
        multi_tf_bias=bias["bias"],
        asset=headline_frame.symbol,
        timeframe=headline_frame.timeframe.value,
        reference_price=decision.reference_price,
        entry=decision.entry,
        stop_loss=decision.stop_loss,
        targets=decision.targets,
        rr_estimate=decision.rr_estimate,
        indicators=decision.extras,
        trend_forecast=trend_forecast,
    )

    # Resolve the actual report folder NOW, lazily. Prefers Downloads /
    # Documents when writable; falls back to the configured project folder
    # and finally to a temp directory. The fallback source is surfaced in
    # the warnings so the user knows the real location.
    location = resolve_report_dir(cfg.report_dir)
    data_quality["report_location"] = {
        "path": str(location.path),
        "source": location.source,
        "note": location.note,
    }
    if location.source == "tempfile":
        warnings.append(location.note)

    try:
        md_path, json_path, report_id = write_analysis_report(
            out_dir=location.path,
            frame=headline_frame,
            decision=decision,
            per_tf_reports=per_tf_reports,
            context_results=context_results,
            data_quality=data_quality,
            bias=bias,
            final_decision=final_decision,
            final_decision_reason=final_reason,
            advisory=advisory,
            trend_forecast=trend_forecast,
        )
    except Exception as e:  # noqa: BLE001
        # A write failure must not take the whole pipeline down. We still
        # return the ResearchResult with blank paths and an explicit warning;
        # the spoken response from the advisory stays valid.
        log.warning("report write failed: %s", e)
        warnings.append(f"report write failed: {type(e).__name__}: {e}")
        md_path, json_path, report_id = "", "", ""

    # Record a forecast ONLY when the FINAL decision has a direction. The
    # strategy-only direction is not actionable if the multi-TF bias
    # disagrees — recording it would pollute the forecast outcomes table.
    if final_decision in ("BUY BIAS", "SHORT BIAS"):
        tracker = ForecastTracker(cfg.db_path)
        try:
            tracker.record(
                asset=headline_frame.symbol,
                source=headline_frame.source,
                timeframe=headline_frame.timeframe.value,
                strategy_name=decision.strategy_name,
                strategy_version=decision.strategy_version,
                direction="LONG" if final_decision == "BUY BIAS" else "SHORT",
                reference_price=decision.reference_price,
                stop_price=decision.stop_loss,
                target_price=decision.targets[0] if decision.targets else None,
                horizon_bars=24,
                heuristic_confidence=decision.heuristic_confidence,
                indicators=decision.extras,
                cancellation_conditions=list(decision.cancellation_conditions),
            )
        finally:
            tracker.close()

    # Belt-and-braces: never claim a path exists unless the file does. If
    # either write silently produced an empty path, drop both so the spoken
    # response does not announce a bogus location.
    md_str = str(md_path) if md_path and Path(md_path).is_file() else ""
    json_str = str(json_path) if json_path and Path(json_path).is_file() else ""

    return ResearchResult(
        asset=headline_frame.symbol,
        conclusion=final_decision,
        strategy_decision=decision.decision,
        multi_tf_bias=bias["bias"],
        final_decision_reason=final_reason,
        heuristic_confidence=decision.heuristic_confidence,
        headline_timeframe=headline_frame.timeframe.value,
        reference_price=decision.reference_price,
        report_markdown_path=md_str,
        report_json_path=json_str,
        report_id=report_id,
        data_quality_summary=f"{len(headline_frame)} bars on {headline_frame.timeframe.value}",
        warnings=tuple(warnings),
        advisory=advisory,
    )


def _data_unavailable_result(
    asset: str, *, reason: str, detail: str, warnings: tuple[str, ...]
) -> "ResearchResult":
    advisory = build_advisory(
        final_decision="NO TRADE",
        final_decision_reason=reason,
        strategy_decision="DATA UNAVAILABLE",
        multi_tf_bias="DATA UNAVAILABLE",
        asset=asset,
        timeframe="",
        reference_price=0.0,
        entry=None,
        stop_loss=None,
        targets=(),
        rr_estimate=None,
        indicators={},
    )
    # Override the Bengali summary to make "data unavailable" explicit — the
    # caller specifically asked for honest reporting of this state.
    bn = (
        f"স্যার, {advisory.asset_display}-এর data এখনই available নয়। "
        "কোনো entry, stop-loss বা target দেওয়া যাচ্ছে না। "
        f"কারণ: {reason}. Data পাওয়া গেলে আবার research করব।"
    )
    advisory = Advisory(
        final_decision="NO TRADE",
        final_decision_reason=reason,
        asset_display=advisory.asset_display,
        timeframe="",
        what_to_do_now_bn="এখনই নতুন trade entry করবেন না। data available নয়।",
        entry_display=advisory.entry_display,
        stop_loss_display=advisory.stop_loss_display,
        target_display=advisory.target_display,
        rr_display=advisory.rr_display,
        reason_bn=f"Data unavailable: {reason}.",
        next_step_bn="data provider স্থিতিশীল হলে আবার research করব।",
        voice_summary_bn=bn,
        raw_levels=advisory.raw_levels,
    )
    return ResearchResult(
        asset=asset,
        conclusion="DATA UNAVAILABLE",
        strategy_decision="DATA UNAVAILABLE",
        multi_tf_bias="DATA UNAVAILABLE",
        final_decision_reason=reason,
        heuristic_confidence=0.0,
        headline_timeframe="",
        reference_price=0.0,
        report_markdown_path="",
        report_json_path="",
        report_id="",
        data_quality_summary=detail,
        warnings=warnings,
        advisory=advisory,
    )


def _tf_rank(tf: Timeframe) -> int:
    order = [
        Timeframe.M1, Timeframe.M5, Timeframe.M15, Timeframe.M30,
        Timeframe.H1, Timeframe.H2, Timeframe.H4, Timeframe.H6,
        Timeframe.H12, Timeframe.D1, Timeframe.D3, Timeframe.W1,
    ]
    return order.index(tf) if tf in order else 0


def _na_context():
    from .analysis.context import ContextResult
    return ContextResult("correlation_with_btc", False, reason="asset is BTC itself")
