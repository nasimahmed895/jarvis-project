"""Report-integrity regression tests.

Guarantees that the following four surfaces NEVER disagree:
    1. ResearchResult.conclusion          — spoken summary input
    2. JSON "final_decision" key
    3. JSON "conclusion" key              — backwards-compatible alias
    4. Markdown final-decision headline

Verified on four scenarios: agreement (long + long), agreement (short +
short), direction conflict, and strategy NO TRADE.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pytest

from market.analysis.multi_tf import TimeframeReport
from market.analysis.regime import RegimeSnapshot
from market.config import MarketConfig, PaperTradingConfig
from market.pipeline import compute_final_decision, research_asset
from market.strategies.base import LONG_BIAS, NO_TRADE, SHORT_BIAS, StrategyDecision
from market.types import Candle, OHLCVFrame, Timeframe, TIMEFRAME_MS
from tests.fixtures.synth import trending_frame


class _ControlledPipelineProvider:
    """Mock provider that returns the same frame for every TF requested."""

    name = "mock"

    def __init__(self, frame_by_tf: dict[Timeframe, OHLCVFrame], supported: set[str]):
        self._by_tf = frame_by_tf
        self._supported = supported

    def ohlcv(self, symbol, timeframe, limit, end_ts_ms=None):
        base = self._by_tf[timeframe]
        now = int(time.time() * 1000)
        step = TIMEFRAME_MS[timeframe]
        last_close = base.candles[-1].close_time_ms
        shift = now - last_close + step // 2
        candles = tuple(
            Candle(
                open_time_ms=c.open_time_ms + shift,
                open=c.open, high=c.high, low=c.low, close=c.close, volume=c.volume,
                close_time_ms=c.close_time_ms + shift,
                is_closed=(c.close_time_ms + shift) <= now,
            )
            for c in base.candles
        )
        return OHLCVFrame(
            symbol=symbol if symbol.endswith("USDT") else symbol + "USDT",
            timeframe=timeframe, source="mock", retrieved_at_ms=now, candles=candles,
        )

    def ticker(self, symbol):
        return {}

    def is_symbol_supported(self, symbol):
        return symbol.upper() in self._supported


def _cfg(tmp_project: Path, tfs_intraday, tfs_swing) -> MarketConfig:
    return MarketConfig(
        provider="mock", quote_currency="USDT",
        assets=("BTC",),
        intraday_timeframes=tuple(tfs_intraday),
        swing_timeframes=tuple(tfs_swing),
        lookback_candles=300, data_staleness_seconds=120,
        report_dir=tmp_project / "reports",
        db_path=tmp_project / "memory" / "market.db",
        log_level="INFO", spot_only=True,
        paper_trading=PaperTradingConfig(10_000.0, 0.01, 3, 0.03, 0.15, 0.001, 5.0),
        base_dir=tmp_project,
    )


def _assert_surfaces_match(result, md_path: str, json_path: str, expected: str) -> None:
    assert result.conclusion == expected
    data = json.loads(Path(json_path).read_text(encoding="utf-8"))
    assert data["final_decision"] == expected
    assert data["conclusion"] == expected
    md = Path(md_path).read_text(encoding="utf-8")
    # Markdown includes a dedicated "Final Decision" heading followed by the
    # exact label — a strict regex guard catches a drift between layers.
    assert re.search(rf"## Final Decision\s+\*\*{re.escape(expected)}\*\*", md)


def test_long_agreement_consistent(tmp_project: Path) -> None:
    frames = {
        Timeframe.H1: trending_frame(300, Timeframe.H1, 100.0, 0.1),
        Timeframe.H4: trending_frame(300, Timeframe.H4, 100.0, 0.1),
    }
    prov = _ControlledPipelineProvider(frames, {"BTC", "BTCUSDT"})
    r = research_asset(
        "BTC", horizon="both",
        config=_cfg(tmp_project, [Timeframe.H1], [Timeframe.H4]),
        provider=prov,
    )
    _assert_surfaces_match(r, r.report_markdown_path, r.report_json_path, r.conclusion)
    # Policy sanity: whichever label the pipeline arrived at, the policy
    # function agrees given the two underlying signals.
    assert compute_final_decision(r.strategy_decision, r.multi_tf_bias)[0] == r.conclusion


def test_short_agreement_consistent(tmp_project: Path) -> None:
    frames = {
        Timeframe.H1: trending_frame(300, Timeframe.H1, 300.0, -0.1),
        Timeframe.H4: trending_frame(300, Timeframe.H4, 300.0, -0.1),
    }
    prov = _ControlledPipelineProvider(frames, {"BTC", "BTCUSDT"})
    r = research_asset(
        "BTC", horizon="both",
        config=_cfg(tmp_project, [Timeframe.H1], [Timeframe.H4]),
        provider=prov,
    )
    _assert_surfaces_match(r, r.report_markdown_path, r.report_json_path, r.conclusion)
    assert compute_final_decision(r.strategy_decision, r.multi_tf_bias)[0] == r.conclusion


def test_conflict_resolves_to_no_trade(tmp_project: Path) -> None:
    # Opposing trends on the two timeframes → multi_tf conflict → NO TRADE.
    frames = {
        Timeframe.H1: trending_frame(300, Timeframe.H1, 100.0, 0.1),   # up
        Timeframe.H4: trending_frame(300, Timeframe.H4, 300.0, -0.1),  # down
    }
    prov = _ControlledPipelineProvider(frames, {"BTC", "BTCUSDT"})
    r = research_asset(
        "BTC", horizon="both",
        config=_cfg(tmp_project, [Timeframe.H1], [Timeframe.H4]),
        provider=prov,
    )
    # Final MUST be NO TRADE regardless of which single-TF strategy decision fired.
    assert r.conclusion == "NO TRADE"
    _assert_surfaces_match(r, r.report_markdown_path, r.report_json_path, "NO TRADE")


def test_strategy_no_trade_overrides_bias(tmp_project: Path) -> None:
    # Flat frames → strategy returns NO TRADE regardless of multi_tf.
    from tests.fixtures.synth import constant_frame

    frames = {
        Timeframe.H1: constant_frame(300, Timeframe.H1),
        Timeframe.H4: constant_frame(300, Timeframe.H4),
    }
    prov = _ControlledPipelineProvider(frames, {"BTC", "BTCUSDT"})
    r = research_asset(
        "BTC", horizon="both",
        config=_cfg(tmp_project, [Timeframe.H1], [Timeframe.H4]),
        provider=prov,
    )
    assert r.conclusion == "NO TRADE"
    _assert_surfaces_match(r, r.report_markdown_path, r.report_json_path, "NO TRADE")
