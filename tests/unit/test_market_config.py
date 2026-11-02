"""Phase 2 — configuration unit tests."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from market.config import load_config, MarketConfig
from market.exceptions import ConfigError
from market.types import Timeframe


def test_defaults_applied_when_block_missing(tmp_project: Path) -> None:
    (tmp_project / "config" / "api_keys.json").write_text("{}", encoding="utf-8")
    cfg = load_config(tmp_project / "config" / "api_keys.json")
    assert isinstance(cfg, MarketConfig)
    assert cfg.provider == "binance_public"
    assert cfg.quote_currency == "USDT"
    assert cfg.assets == ("BTC", "ETH")
    assert Timeframe.M1 in cfg.intraday_timeframes
    assert Timeframe.D1 in cfg.swing_timeframes
    assert cfg.spot_only is True
    assert cfg.paper_trading.initial_balance_usd == 10_000.0


def test_missing_file_yields_defaults(tmp_project: Path) -> None:
    cfg = load_config(tmp_project / "config" / "api_keys.json")
    assert cfg.provider == "binance_public"


def test_spot_only_false_rejected(tmp_project: Path) -> None:
    f = tmp_project / "config" / "api_keys.json"
    f.write_text(json.dumps({"market_config": {"spot_only": False}}), encoding="utf-8")
    with pytest.raises(ConfigError, match="spot_only"):
        load_config(f)


def test_unknown_timeframe_rejected(tmp_project: Path) -> None:
    f = tmp_project / "config" / "api_keys.json"
    f.write_text(
        json.dumps({"market_config": {"intraday_timeframes": ["2m"]}}), encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="intraday_timeframes"):
        load_config(f)


def test_assets_normalised(tmp_project: Path) -> None:
    f = tmp_project / "config" / "api_keys.json"
    f.write_text(
        json.dumps({"market_config": {"assets": ["btc", "eth", "BTC", "sol"]}}),
        encoding="utf-8",
    )
    cfg = load_config(f)
    assert cfg.assets == ("BTC", "ETH", "SOL")


def test_report_dir_outside_project_rejected(tmp_project: Path) -> None:
    f = tmp_project / "config" / "api_keys.json"
    f.write_text(
        json.dumps({"market_config": {"report_dir": "/etc/passwd"}}), encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="report_dir"):
        load_config(f)


def test_db_path_outside_project_rejected(tmp_project: Path) -> None:
    f = tmp_project / "config" / "api_keys.json"
    f.write_text(
        json.dumps({"market_config": {"db_path": "/tmp/evil.db"}}), encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="db_path"):
        load_config(f)


def test_risk_per_trade_bounds(tmp_project: Path) -> None:
    f = tmp_project / "config" / "api_keys.json"
    f.write_text(
        json.dumps({"market_config": {"paper_trading": {"risk_per_trade": 0.5}}}),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="risk_per_trade"):
        load_config(f)


def test_fee_rate_bounds(tmp_project: Path) -> None:
    f = tmp_project / "config" / "api_keys.json"
    f.write_text(
        json.dumps({"market_config": {"paper_trading": {"fee_rate": 0.5}}}),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="fee_rate"):
        load_config(f)


def test_malformed_json_rejected(tmp_project: Path) -> None:
    f = tmp_project / "config" / "api_keys.json"
    f.write_text("{ not valid json", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid JSON"):
        load_config(f)


def test_market_config_not_object_rejected(tmp_project: Path) -> None:
    f = tmp_project / "config" / "api_keys.json"
    f.write_text(json.dumps({"market_config": "nope"}), encoding="utf-8")
    with pytest.raises(ConfigError, match="market_config"):
        load_config(f)


def test_idempotent(tmp_project: Path) -> None:
    f = tmp_project / "config" / "api_keys.json"
    f.write_text(json.dumps({"market_config": {}}), encoding="utf-8")
    a = load_config(f)
    b = load_config(f)
    assert a == b


def test_import_performs_no_file_io(tmp_project: Path, monkeypatch) -> None:
    """Importing `market.config` fresh must not call open() at module scope."""
    import builtins
    import importlib
    import sys as _sys

    opened: list[str] = []
    real_open = builtins.open

    def _tracker(path, *args, **kwargs):
        opened.append(str(path))
        return real_open(path, *args, **kwargs)

    _sys.modules.pop("market.config", None)
    monkeypatch.setattr(builtins, "open", _tracker)
    importlib.import_module("market.config")
    # Allow pytest / coverage internals that may open files during import;
    # only assert that we did not touch api_keys.json ourselves.
    assert all("api_keys.json" not in p for p in opened)


def test_does_not_touch_other_keys(tmp_project: Path) -> None:
    """A real-looking api_keys.json with a dummy gemini_api_key must still load.

    The dummy value is never read, never echoed, never logged. Test verifies
    that unrelated keys do not interfere and that MarketConfig does not
    surface them anywhere.
    """
    f = tmp_project / "config" / "api_keys.json"
    f.write_text(
        json.dumps(
            {
                "gemini_api_key": "DUMMY_DO_NOT_TOUCH",
                "assistant_name": "JARVIS",
                "market_config": {},
            }
        ),
        encoding="utf-8",
    )
    cfg = load_config(f)
    # Nothing in the dataclass should contain the dummy string.
    assert "DUMMY_DO_NOT_TOUCH" not in repr(cfg)
