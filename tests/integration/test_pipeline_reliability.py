"""Reliability tests — the pipeline survives unusual conditions.

* A write failure returns a ResearchResult with blank paths and a warning,
  never a bogus path announced as if it existed.
* The spoken Bengali advisory is still produced when the report cannot be
  written.
* A tempfile fallback is reflected in the warnings so the user knows the
  real location.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from market.config import MarketConfig, PaperTradingConfig
from market.pipeline import research_asset
from market.reports import location as loc
from market.types import Candle, OHLCVFrame, Timeframe, TIMEFRAME_MS
from tests.fixtures.synth import trending_frame


class _Provider:
    name = "mock"

    def __init__(self, frames):
        self._frames = frames

    def ohlcv(self, symbol, tf, limit, end_ts_ms=None):
        base = self._frames[tf]
        now = int(time.time() * 1000)
        step = TIMEFRAME_MS[tf]
        shift = now - base.candles[-1].close_time_ms + step // 2
        candles = tuple(
            Candle(c.open_time_ms + shift, c.open, c.high, c.low, c.close, c.volume,
                   c.close_time_ms + shift, (c.close_time_ms + shift) <= now)
            for c in base.candles
        )
        return OHLCVFrame(
            symbol=symbol if symbol.endswith("USDT") else symbol + "USDT",
            timeframe=tf, source="mock", retrieved_at_ms=now, candles=candles,
        )

    def ticker(self, s): return {}
    def is_symbol_supported(self, s): return s.upper() in ("BTC", "BTCUSDT")


def _cfg(tmp_project):
    return MarketConfig(
        provider="mock", quote_currency="USDT", assets=("BTC",),
        intraday_timeframes=(Timeframe.H1,), swing_timeframes=(Timeframe.H4,),
        lookback_candles=300, data_staleness_seconds=120,
        report_dir=tmp_project / "reports",
        db_path=tmp_project / "memory" / "m.db",
        log_level="INFO", spot_only=True,
        paper_trading=PaperTradingConfig(10_000.0, 0.01, 3, 0.03, 0.15, 0.001, 5.0),
        base_dir=tmp_project,
    )


def test_write_failure_yields_blank_paths_but_valid_advisory(
    tmp_project: Path, monkeypatch
) -> None:
    frames = {
        Timeframe.H1: trending_frame(300, Timeframe.H1, 100.0, 0.1),
        Timeframe.H4: trending_frame(300, Timeframe.H4, 100.0, 0.1),
    }
    # Force the writer to raise, simulating a disk-full / permission error.
    import market.pipeline as pipeline_mod
    import market.reports.analysis_report as ar_mod

    def _boom(**kw):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(ar_mod, "write_analysis_report", _boom)
    monkeypatch.setattr(pipeline_mod, "write_analysis_report", _boom)

    r = research_asset("BTC", horizon="both",
                       config=_cfg(tmp_project), provider=_Provider(frames))
    assert r.report_markdown_path == ""
    assert r.report_json_path == ""
    assert r.advisory is not None
    assert r.advisory.voice_summary_bn
    assert any("report write failed" in w for w in r.warnings)


def test_tempfile_fallback_recorded_in_warnings(
    tmp_project: Path, monkeypatch
) -> None:
    frames = {
        Timeframe.H1: trending_frame(300, Timeframe.H1, 100.0, 0.1),
        Timeframe.H4: trending_frame(300, Timeframe.H4, 100.0, 0.1),
    }
    # Force the resolver to pick the tempfile branch.
    import market.pipeline as pipeline_mod

    from market.reports.location import ResolvedLocation
    import tempfile
    tmp_dir = Path(tempfile.gettempdir()) / "JARVIS Reports"

    def _force_tempfile(configured):
        tmp_dir.mkdir(parents=True, exist_ok=True)
        return ResolvedLocation(
            path=tmp_dir, source="tempfile",
            note="Preferred locations were unwritable; saved to a temp folder.",
        )

    monkeypatch.setattr(pipeline_mod, "resolve_report_dir", _force_tempfile)

    r = research_asset("BTC", horizon="both",
                       config=_cfg(tmp_project), provider=_Provider(frames))
    assert any("temp folder" in w.lower() for w in r.warnings)


def test_report_paths_only_announced_when_files_exist(tmp_project: Path) -> None:
    frames = {
        Timeframe.H1: trending_frame(300, Timeframe.H1, 100.0, 0.1),
        Timeframe.H4: trending_frame(300, Timeframe.H4, 100.0, 0.1),
    }
    r = research_asset("BTC", horizon="both",
                       config=_cfg(tmp_project), provider=_Provider(frames))
    if r.report_markdown_path:
        assert Path(r.report_markdown_path).exists()
    if r.report_json_path:
        assert Path(r.report_json_path).exists()
