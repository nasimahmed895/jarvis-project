"""Pipeline integration: trend forecast flows into advisory, JSON, Markdown."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from market.config import MarketConfig, PaperTradingConfig
from market.pipeline import research_asset
from market.types import Candle, OHLCVFrame, Timeframe, TIMEFRAME_MS
from tests.fixtures.synth import trending_frame


class _Prov:
    name = "mock"
    def __init__(self, frames):
        self._f = frames
    def ohlcv(self, s, tf, limit, end_ts_ms=None):
        base = self._f[tf]
        now = int(time.time() * 1000)
        step = TIMEFRAME_MS[tf]
        shift = now - base.candles[-1].close_time_ms + step // 2
        candles = tuple(
            Candle(c.open_time_ms + shift, c.open, c.high, c.low, c.close, c.volume,
                   c.close_time_ms + shift, (c.close_time_ms + shift) <= now)
            for c in base.candles
        )
        return OHLCVFrame(
            symbol=s if s.endswith("USDT") else s + "USDT",
            timeframe=tf, source="mock", retrieved_at_ms=now, candles=candles,
        )
    def ticker(self, s): return {}
    def is_symbol_supported(self, s): return s.upper() in ("BTC", "BTCUSDT")


def _cfg(tmp_project):
    return MarketConfig(
        provider="mock", quote_currency="USDT", assets=("BTC",),
        intraday_timeframes=(Timeframe.M15, Timeframe.H1),
        swing_timeframes=(Timeframe.H4, Timeframe.D1),
        lookback_candles=300, data_staleness_seconds=120,
        report_dir=tmp_project / "reports",
        db_path=tmp_project / "memory" / "m.db",
        log_level="INFO", spot_only=True,
        paper_trading=PaperTradingConfig(10_000.0, 0.01, 3, 0.03, 0.15, 0.001, 5.0),
        base_dir=tmp_project,
    )


def test_pipeline_emits_trend_forecast_block(tmp_project: Path) -> None:
    frames = {
        Timeframe.M15: trending_frame(300, Timeframe.M15, 100.0, 0.1),
        Timeframe.H1:  trending_frame(300, Timeframe.H1, 100.0, 0.1),
        Timeframe.H4:  trending_frame(300, Timeframe.H4, 100.0, 0.1),
        Timeframe.D1:  trending_frame(300, Timeframe.D1, 100.0, 0.1),
    }
    r = research_asset("BTC", horizon="both",
                       config=_cfg(tmp_project), provider=_Prov(frames))
    assert r.report_json_path
    data = json.loads(Path(r.report_json_path).read_text(encoding="utf-8"))
    assert data["trend_forecast"] is not None
    tf = data["trend_forecast"]
    assert tf["current_state"] in ("UPTREND", "DOWNTREND", "RANGING", "UNCERTAIN")
    assert tf["short_term"] is not None
    assert tf["medium_term"] is not None
    assert tf["scenarios"] and len(tf["scenarios"]) == 3
    assert tf["hold_assessment"]["audience"] == "already_holding"
    assert tf["new_entry_assessment"]["audience"] == "new_entry"
    assert tf["heuristic_confidence"] in ("low", "moderate", "qualitative-only")
    assert "scenario" in tf["horizon_disclaimer"].lower()


def test_markdown_contains_trend_forecast_section(tmp_project: Path) -> None:
    frames = {
        Timeframe.M15: trending_frame(300, Timeframe.M15, 100.0, 0.1),
        Timeframe.H1:  trending_frame(300, Timeframe.H1, 100.0, 0.1),
        Timeframe.H4:  trending_frame(300, Timeframe.H4, 100.0, 0.1),
        Timeframe.D1:  trending_frame(300, Timeframe.D1, 100.0, 0.1),
    }
    r = research_asset("BTC", horizon="both",
                       config=_cfg(tmp_project), provider=_Prov(frames))
    md = Path(r.report_markdown_path).read_text(encoding="utf-8")
    assert "## Trend Forecast and Spot Position Guidance" in md
    assert "### Scenarios" in md
    assert "Hold-position assessment" in md
    assert "New-entry assessment" in md


def test_bengali_voice_summary_includes_trend_forecast(tmp_project: Path) -> None:
    frames = {
        Timeframe.M15: trending_frame(300, Timeframe.M15, 100.0, 0.1),
        Timeframe.H1:  trending_frame(300, Timeframe.H1, 100.0, 0.1),
        Timeframe.H4:  trending_frame(300, Timeframe.H4, 100.0, 0.1),
        Timeframe.D1:  trending_frame(300, Timeframe.D1, 100.0, 0.1),
    }
    r = research_asset("BTC", horizon="both",
                       config=_cfg(tmp_project), provider=_Prov(frames))
    assert "Trend forecast" in r.advisory.voice_summary_bn
    # Horizon qualifier in Bengali summary.
    assert "scenario" in r.advisory.voice_summary_bn.lower() or "guarantee নয়" in r.advisory.voice_summary_bn


def test_missing_timeframes_surface_in_report(tmp_project: Path) -> None:
    # Only 1h + 4h supplied; 15m + 1d missing.
    frames = {
        Timeframe.H1: trending_frame(300, Timeframe.H1, 100.0, 0.1),
        Timeframe.H4: trending_frame(300, Timeframe.H4, 100.0, 0.1),
    }
    cfg = _cfg(tmp_project)
    # Force horizon to use only these two TFs so build_trend_forecast reports
    # 15m and 1d as missing.
    cfg = MarketConfig(
        provider=cfg.provider, quote_currency=cfg.quote_currency,
        assets=cfg.assets,
        intraday_timeframes=(Timeframe.H1,),
        swing_timeframes=(Timeframe.H4,),
        lookback_candles=cfg.lookback_candles,
        data_staleness_seconds=cfg.data_staleness_seconds,
        report_dir=cfg.report_dir, db_path=cfg.db_path,
        log_level=cfg.log_level, spot_only=cfg.spot_only,
        paper_trading=cfg.paper_trading, base_dir=cfg.base_dir,
    )
    r = research_asset("BTC", horizon="both", config=cfg, provider=_Prov(frames))
    data = json.loads(Path(r.report_json_path).read_text(encoding="utf-8"))
    assert "15m" in data["trend_forecast"]["missing_timeframes"]
    assert "1d" in data["trend_forecast"]["missing_timeframes"]


def test_no_percentage_leaks_into_voice_summary(tmp_project: Path) -> None:
    frames = {
        Timeframe.M15: trending_frame(300, Timeframe.M15, 100.0, 0.1),
        Timeframe.H1:  trending_frame(300, Timeframe.H1, 100.0, 0.1),
        Timeframe.H4:  trending_frame(300, Timeframe.H4, 100.0, 0.1),
        Timeframe.D1:  trending_frame(300, Timeframe.D1, 100.0, 0.1),
    }
    r = research_asset("BTC", horizon="both",
                       config=_cfg(tmp_project), provider=_Prov(frames))
    # The forecast section does not emit numeric probability percentages.
    import re
    assert re.search(r"\d+\s*%\s*(chance|probability)", r.advisory.voice_summary_bn, re.IGNORECASE) is None
