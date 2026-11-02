"""Phase 11 — end-to-end pipeline with a mocked provider."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from market.config import MarketConfig, PaperTradingConfig
from market.pipeline import research_asset
from market.types import Timeframe
from tests.fixtures.synth import trending_frame


class _MockProvider:
    name = "mock"

    def __init__(self, frame_by_tf: dict, supported: set[str]):
        self._by_tf = frame_by_tf
        self._supported = supported

    def ohlcv(self, symbol, timeframe, limit, end_ts_ms=None):
        now_ms = int(time.time() * 1000)
        base = self._by_tf[timeframe]
        # Shift candles so staleness check passes (last close near "now").
        from market.types import Candle, OHLCVFrame, TIMEFRAME_MS

        step = TIMEFRAME_MS[timeframe]
        last_close = base.candles[-1].close_time_ms
        shift = now_ms - last_close + step // 2
        candles = tuple(
            Candle(
                open_time_ms=c.open_time_ms + shift,
                open=c.open, high=c.high, low=c.low, close=c.close, volume=c.volume,
                close_time_ms=c.close_time_ms + shift,
                is_closed=(c.close_time_ms + shift) <= now_ms,
            )
            for c in base.candles
        )
        return OHLCVFrame(
            symbol=symbol if symbol.endswith("USDT") else f"{symbol}USDT",
            timeframe=timeframe,
            source="mock",
            retrieved_at_ms=now_ms,
            candles=candles,
        )

    def ticker(self, symbol):
        return {}

    def is_symbol_supported(self, symbol):
        return symbol.upper() in self._supported


def _cfg(tmp_project: Path) -> MarketConfig:
    return MarketConfig(
        provider="mock",
        quote_currency="USDT",
        assets=("BTC",),
        intraday_timeframes=(Timeframe.H1,),
        swing_timeframes=(Timeframe.H4,),
        lookback_candles=300,
        data_staleness_seconds=86_400,
        report_dir=tmp_project / "reports",
        db_path=tmp_project / "memory" / "market.db",
        log_level="INFO",
        spot_only=True,
        paper_trading=PaperTradingConfig(
            initial_balance_usd=10_000.0,
            risk_per_trade=0.01,
            max_concurrent_positions=3,
            max_daily_loss=0.03,
            max_drawdown=0.15,
            fee_rate=0.001,
            slippage_bps=5.0,
        ),
        base_dir=tmp_project,
    )


def test_pipeline_produces_report(tmp_project: Path) -> None:
    frames = {
        Timeframe.H1: trending_frame(300, Timeframe.H1, start_price=100, step_price=0.1),
        Timeframe.H4: trending_frame(300, Timeframe.H4, start_price=100, step_price=0.1),
    }
    provider = _MockProvider(frames, supported={"BTC", "BTCUSDT"})
    r = research_asset("BTC", horizon="both", config=_cfg(tmp_project), provider=provider)
    assert r.conclusion in ("BUY BIAS", "SHORT BIAS", "NO TRADE")
    assert Path(r.report_markdown_path).exists()
    assert Path(r.report_json_path).exists()
    assert r.report_id

    # Spoken summary (ResearchResult.conclusion) must equal the on-disk
    # report's conclusion. Previously the pipeline fell back to multi-TF
    # bias when the strategy returned NO TRADE, which could make the two
    # disagree. Guard against regression.
    import json
    data = json.loads(Path(r.report_json_path).read_text(encoding="utf-8"))
    assert data["conclusion"] == r.conclusion
    assert data["report_id"] == r.report_id


def test_pipeline_unsupported_symbol(tmp_project: Path) -> None:
    provider = _MockProvider(frame_by_tf={}, supported=set())
    r = research_asset("ZZZ", horizon="both", config=_cfg(tmp_project), provider=provider)
    assert r.conclusion == "DATA UNAVAILABLE"


def test_pipeline_provider_failure(tmp_project: Path) -> None:
    class _BoomProvider:
        name = "boom"
        def ohlcv(self, *a, **kw): raise RuntimeError("down")
        def ticker(self, *a, **kw): return {}
        def is_symbol_supported(self, *a, **kw): return True
    r = research_asset("BTC", horizon="both", config=_cfg(tmp_project), provider=_BoomProvider())
    assert r.conclusion == "DATA UNAVAILABLE"
