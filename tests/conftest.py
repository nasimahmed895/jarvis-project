"""Shared pytest fixtures for the market-package test suite.

Fixtures here MUST NOT touch the real `config/api_keys.json`, the real
`memory/long_term.json`, or the real database. Every test uses a tmp_path.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def tmp_config(tmp_path: Path) -> Path:
    """Create a minimal valid market_config file inside tmp_path and return it."""
    cfg = {
        "market_config": {
            "report_dir": "reports",
            "db_path": "memory/market.db",
        }
    }
    f = tmp_path / "api_keys.json"
    f.write_text(json.dumps(cfg), encoding="utf-8")
    return f


@pytest.fixture
def tmp_project(tmp_path: Path, monkeypatch) -> Path:
    """Pretend the project root is tmp_path. Resets config / db / reports / logs
    to live under tmp_path so no test can write into the real project tree.
    """
    (tmp_path / "config").mkdir()
    (tmp_path / "memory").mkdir()
    (tmp_path / "reports").mkdir()
    (tmp_path / "logs").mkdir()
    import market.config as _mc

    monkeypatch.setattr(_mc, "_project_root", lambda: tmp_path)
    return tmp_path
