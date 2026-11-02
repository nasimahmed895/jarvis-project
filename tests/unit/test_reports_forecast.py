"""Phase 10 — reports + forecast tracker tests."""
from __future__ import annotations

import json
from pathlib import Path

from market.analysis.context import ContextResult
from market.analysis.multi_tf import analyse_timeframe
from market.forecast.tracker import ForecastTracker
from market.reports.analysis_report import write_analysis_report
from market.reports.paths import report_paths
from market.strategies.registry import get
from market.strategies import ma_trend_v1  # noqa: F401
from market.types import Timeframe
from tests.fixtures.synth import trending_frame


def test_report_paths_unique_and_safe(tmp_path: Path) -> None:
    md1, js1, id1 = report_paths(tmp_path, "BTC")
    md2, js2, id2 = report_paths(tmp_path, "BTC")
    assert id1 != id2
    assert md1.parent == tmp_path and js1.parent == tmp_path


def test_write_analysis_report(tmp_path: Path) -> None:
    s = get("ma_trend_momentum")
    f = trending_frame(200, Timeframe.H1, start_price=100.0, step_price=0.1)
    d = s.evaluate(f, context={})
    tf_rep = [analyse_timeframe(f)]
    md, js, rid = write_analysis_report(
        out_dir=tmp_path,
        frame=f,
        decision=d,
        per_tf_reports=tf_rep,
        context_results=[ContextResult("news_sentiment", False, reason="disabled")],
        data_quality={"received": len(f), "stale_age_seconds": 0.0, "warnings": []},
        bias={"bias": d.decision, "agreement": True, "conflict": False, "per_tf": []},
        final_decision=d.decision,
        final_decision_reason="test",
    )
    assert md.exists() and js.exists()
    data = json.loads(js.read_text(encoding="utf-8"))
    assert data["report_id"] == rid
    assert data["asset"] == f.symbol
    assert "evidence" in data
    text = md.read_text(encoding="utf-8")
    assert "Market Analysis" in text


def test_forecast_record_and_evaluate(tmp_path: Path) -> None:
    t = ForecastTracker(tmp_path / "f.db")
    try:
        rec = t.record(
            asset="BTCUSDT",
            source="binance_public",
            timeframe="1h",
            strategy_name="ma_trend_momentum",
            strategy_version="1.0.0",
            direction="LONG",
            reference_price=20_000.0,
            stop_price=19_500.0,
            target_price=21_000.0,
            horizon_bars=24,
            heuristic_confidence=0.6,
            indicators={"rsi": 55.0},
            cancellation_conditions=["EMA20 crosses EMA50"],
        )
        res = t.evaluate(rec.forecast_id, terminal_price=20_800.0)
        assert res["directionally_correct"] is True
        summary = t.accuracy_summary("ma_trend_momentum")
        assert summary["total"] == 1 and summary["correct"] == 1
    finally:
        t.close()


def test_forecast_wrong_direction(tmp_path: Path) -> None:
    t = ForecastTracker(tmp_path / "f.db")
    try:
        rec = t.record(
            asset="BTCUSDT",
            source="binance_public",
            timeframe="1h",
            strategy_name="s",
            strategy_version="1.0.0",
            direction="LONG",
            reference_price=20_000.0,
            stop_price=None,
            target_price=None,
            horizon_bars=24,
            heuristic_confidence=0.5,
            indicators={},
            cancellation_conditions=[],
        )
        res = t.evaluate(rec.forecast_id, terminal_price=19_500.0)
        assert res["directionally_correct"] is False
    finally:
        t.close()
