"""Phase 2 — logging facade tests."""
from __future__ import annotations

import logging
from pathlib import Path

from market import logging_setup


def test_get_logger_isolated_from_root(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(logging_setup, "_configured", False)
    monkeypatch.setattr(logging_setup, "_logs_dir", lambda: tmp_path)
    log = logging_setup.get_logger("test")
    log.info("hello")
    # Market logger must not propagate to root.
    assert logging.getLogger("market").propagate is False
    out = (tmp_path / "market.log").read_text(encoding="utf-8")
    assert "hello" in out


def test_child_logger_shares_handlers(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(logging_setup, "_configured", False)
    monkeypatch.setattr(logging_setup, "_logs_dir", lambda: tmp_path)
    parent = logging_setup.get_logger()
    child = logging_setup.get_logger("sub")
    assert child.parent is parent
