"""Market-package configuration — loaded ONLY on explicit `load_config()` call.

Importing this module performs no file I/O. The existing JARVIS application is
unaffected if `config/api_keys.json` lacks a `market_config` block or if that
block is malformed; errors surface only when a caller inside the market
package asks for the configuration.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .exceptions import ConfigError
from .types import Timeframe


def _project_root() -> Path:
    """Repo root, resolved once at call time (never at import time)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


_SUPPORTED_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


@dataclass(frozen=True)
class PaperTradingConfig:
    initial_balance_usd: float
    risk_per_trade: float
    max_concurrent_positions: int
    max_daily_loss: float
    max_drawdown: float
    fee_rate: float
    slippage_bps: float


@dataclass(frozen=True)
class MarketConfig:
    provider: str
    quote_currency: str
    assets: tuple[str, ...]
    intraday_timeframes: tuple[Timeframe, ...]
    swing_timeframes: tuple[Timeframe, ...]
    lookback_candles: int
    data_staleness_seconds: int
    report_dir: Path
    db_path: Path
    log_level: str
    spot_only: bool
    paper_trading: PaperTradingConfig
    # Not persisted — resolved from the config file's own location.
    base_dir: Path = field(default_factory=_project_root)


_DEFAULTS: dict[str, Any] = {
    "provider": "binance_public",
    "quote_currency": "USDT",
    "assets": ["BTC", "ETH"],
    "intraday_timeframes": ["1m", "5m", "15m", "1h"],
    "swing_timeframes": ["4h", "1d"],
    "lookback_candles": 1000,
    "data_staleness_seconds": 120,
    "report_dir": "reports",
    "db_path": "memory/market.db",
    "log_level": "INFO",
    "spot_only": True,
    "paper_trading": {
        "initial_balance_usd": 10000.0,
        "risk_per_trade": 0.01,
        "max_concurrent_positions": 3,
        "max_daily_loss": 0.03,
        "max_drawdown": 0.15,
        "fee_rate": 0.001,
        "slippage_bps": 5.0,
    },
}


def _assert_in(name: str, value: Any, choices: set[str] | tuple[str, ...]) -> None:
    if value not in choices:
        raise ConfigError(f"{name}={value!r} is not one of {sorted(choices)}")


def _normalise_assets(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, (list, tuple)):
        raise ConfigError("assets must be a list")
    out: list[str] = []
    seen: set[str] = set()
    for a in raw:
        if not isinstance(a, str):
            raise ConfigError(f"asset entry must be a string: {a!r}")
        s = a.strip().upper()
        if not s or not s.replace("_", "").isalnum() or not (2 <= len(s) <= 20):
            raise ConfigError(f"asset {a!r} is not a valid symbol code")
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
    if not out:
        raise ConfigError("assets list is empty after normalisation")
    return tuple(out)


def _parse_timeframes(raw: Any, field_name: str) -> tuple[Timeframe, ...]:
    if not isinstance(raw, (list, tuple)):
        raise ConfigError(f"{field_name} must be a list of timeframe strings")
    out: list[Timeframe] = []
    for t in raw:
        try:
            out.append(Timeframe.from_str(t))
        except ValueError as e:
            raise ConfigError(f"{field_name}: {e}") from None
    if not out:
        raise ConfigError(f"{field_name} is empty")
    return tuple(out)


def _validate_path_inside(base: Path, candidate: Path, name: str) -> Path:
    """Resolve `candidate` and assert it is inside `base`. Rejects traversal."""
    resolved = (base / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
    try:
        resolved.relative_to(base.resolve())
    except ValueError:
        raise ConfigError(f"{name}={candidate!r} resolves outside the project root")
    return resolved


def _build_paper(raw: Any) -> PaperTradingConfig:
    if not isinstance(raw, dict):
        raise ConfigError("paper_trading must be an object")
    merged = {**_DEFAULTS["paper_trading"], **raw}

    initial = float(merged["initial_balance_usd"])
    if not 0 < initial <= 1_000_000_000:
        raise ConfigError("paper_trading.initial_balance_usd out of range")

    risk = float(merged["risk_per_trade"])
    if not 0 < risk <= 0.1:
        raise ConfigError("paper_trading.risk_per_trade must be in (0, 0.1]")

    max_pos = int(merged["max_concurrent_positions"])
    if not 1 <= max_pos <= 50:
        raise ConfigError("paper_trading.max_concurrent_positions out of range")

    daily = float(merged["max_daily_loss"])
    if not 0 < daily <= 0.5:
        raise ConfigError("paper_trading.max_daily_loss must be in (0, 0.5]")

    dd = float(merged["max_drawdown"])
    if not 0 < dd <= 0.5:
        raise ConfigError("paper_trading.max_drawdown must be in (0, 0.5]")

    fee = float(merged["fee_rate"])
    if not 0 <= fee <= 0.01:
        raise ConfigError("paper_trading.fee_rate must be in [0, 0.01]")

    slip = float(merged["slippage_bps"])
    if not 0 <= slip <= 50:
        raise ConfigError("paper_trading.slippage_bps must be in [0, 50]")

    return PaperTradingConfig(
        initial_balance_usd=initial,
        risk_per_trade=risk,
        max_concurrent_positions=max_pos,
        max_daily_loss=daily,
        max_drawdown=dd,
        fee_rate=fee,
        slippage_bps=slip,
    )


def load_config(config_path: Path | None = None) -> MarketConfig:
    """Load, validate, and return a `MarketConfig`.

    `config_path` points at the project's `config/api_keys.json` (same file
    JARVIS already uses). Only the `market_config` subkey is read — never
    `gemini_api_key`, never any other field. A missing file or missing
    `market_config` block yields the full default configuration.

    Any schema violation raises `ConfigError` with the offending key named.
    """
    base = _project_root()
    path = config_path if config_path is not None else (base / "config" / "api_keys.json")

    raw: dict[str, Any] = {}
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            raise ConfigError(f"config file is not valid JSON: {e}") from None
        except OSError as e:
            raise ConfigError(f"cannot read config file: {e}") from None
        if not isinstance(data, dict):
            raise ConfigError("config file root is not a JSON object")
        mc = data.get("market_config")
        if isinstance(mc, dict):
            raw = mc
        elif mc is not None:
            raise ConfigError("market_config must be an object")

    merged: dict[str, Any] = {**{k: v for k, v in _DEFAULTS.items() if k != "paper_trading"}, **raw}

    provider = str(merged["provider"]).strip().lower()
    if not provider or len(provider) > 64:
        raise ConfigError("provider name is empty or too long")

    quote = str(merged["quote_currency"]).strip().upper()
    if not (2 <= len(quote) <= 10) or not quote.isalpha():
        raise ConfigError("quote_currency must be 2-10 alphabetic characters")

    assets = _normalise_assets(merged["assets"])
    intraday = _parse_timeframes(merged["intraday_timeframes"], "intraday_timeframes")
    swing = _parse_timeframes(merged["swing_timeframes"], "swing_timeframes")

    lookback = int(merged["lookback_candles"])
    if not 100 <= lookback <= 5000:
        raise ConfigError("lookback_candles must be in [100, 5000]")

    stale = int(merged["data_staleness_seconds"])
    if not 10 <= stale <= 86_400:
        raise ConfigError("data_staleness_seconds must be in [10, 86400]")

    log_level = str(merged["log_level"]).strip().upper()
    _assert_in("log_level", log_level, _SUPPORTED_LOG_LEVELS)

    spot_only = bool(merged["spot_only"])
    if not spot_only:
        raise ConfigError(
            "spot_only=false is not permitted. v1 of this system is spot-only "
            "and will not place or simulate derivatives trades."
        )

    report_dir = _validate_path_inside(base, Path(str(merged["report_dir"])), "report_dir")
    db_path = _validate_path_inside(base, Path(str(merged["db_path"])), "db_path")

    paper = _build_paper(raw.get("paper_trading", _DEFAULTS["paper_trading"]))

    return MarketConfig(
        provider=provider,
        quote_currency=quote,
        assets=assets,
        intraday_timeframes=intraday,
        swing_timeframes=swing,
        lookback_candles=lookback,
        data_staleness_seconds=stale,
        report_dir=report_dir,
        db_path=db_path,
        log_level=log_level,
        spot_only=True,
        paper_trading=paper,
        base_dir=base,
    )
