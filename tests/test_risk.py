"""Tests for core/risk.py — the explainable risk aggregator."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from core import risk


def test_evaluate_returns_list_without_crashing(tmp_path, monkeypatch):
    # No tasks DB, no schedules, no predictions → empty but doesn't crash.
    # Point each sub-module at an empty tmp path.
    from core import tasks as _t
    _t._DB = None
    _t.connect(tmp_path / "tasks.sqlite")
    out = risk.evaluate()
    assert isinstance(out, list)


def test_overdue_task_surfaces_risk(tmp_path):
    from core import tasks as _t
    _t._DB = None
    _t.connect(tmp_path / "tasks.sqlite")
    tid = _t.create_task("ship a thing")
    _t.set_task_status(tid, "running")
    # Force updated_at back by ~3 days.
    _t._DB.execute(
        "UPDATE tasks SET updated_at=? WHERE id=?",
        (time.time() - 72 * 3600, tid))
    out = risk.evaluate()
    stale = [r for r in out if r.source == "risk.rule_overdue_tasks"]
    assert stale, "overdue rule should surface a risk"
    r = stale[0]
    assert r.severity in ("medium", "high")
    assert r.evidence[0]["task_id"] == tid


def test_register_rule_picked_up():
    call_count = {"n": 0}

    def _custom(now):
        call_count["n"] += 1
        return [risk.Risk(category="infra", title="fake",
                          severity="low", source="custom")]
    try:
        risk.register_rule(_custom)
        out = risk.evaluate()
        assert call_count["n"] >= 1
        assert any(r.source == "custom" for r in out)
    finally:
        risk._RULES.remove(_custom)


def test_broken_rule_does_not_sink_the_report():
    def _broken(now):
        raise RuntimeError("boom")
    try:
        risk.register_rule(_broken)
        out = risk.evaluate()
        # The broken rule yielded a synthetic infra risk, didn't raise.
        assert any(r.title.startswith("Risk rule crashed") for r in out)
    finally:
        risk._RULES.remove(_broken)
