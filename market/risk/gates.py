"""Pre-trade risk gates.

Every proposed trade is evaluated against a layered set of checks. A single
failure rejects the trade with an explicit reason code.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..exceptions import RiskRejection
from .sizer import SizeResult, size_position


@dataclass(frozen=True, slots=True)
class RiskParams:
    risk_per_trade: float
    max_concurrent_positions: int
    max_daily_loss_frac: float
    max_drawdown_frac: float
    min_rr: float
    max_spread_bps: float
    max_data_age_seconds: float
    fee_rate: float
    slippage_bps: float


@dataclass(frozen=True, slots=True)
class AccountState:
    equity_usd: float
    available_cash_usd: float
    open_positions: int
    realised_pnl_today_usd: float
    peak_equity_usd: float


@dataclass(frozen=True, slots=True)
class RiskDecision:
    accepted: bool
    code: str
    reason: str
    size: SizeResult | None = None


_CODES = {
    "OK": "accepted",
    "NO_STOP": "missing or invalid stop_loss",
    "BAD_RR": "risk-to-reward below minimum",
    "STALE": "market data too old",
    "SPREAD": "spread too wide",
    "DAILY_LOSS": "daily loss limit exceeded",
    "DRAWDOWN": "drawdown limit exceeded",
    "MAX_POS": "concurrent position limit reached",
    "SIZE_ZERO": "sizer returned zero quantity",
    "BAD_SIDE": "entry/stop inconsistent with declared side",
    "NO_EQUITY": "non-positive equity",
}


def _rr(entry: float, stop: float, target: float, side: str) -> float:
    if side == "LONG":
        risk = entry - stop
        reward = target - entry
    else:
        risk = stop - entry
        reward = entry - target
    if risk <= 0 or reward <= 0:
        return 0.0
    return reward / risk


def check_trade(
    *,
    side: str,                   # "LONG" | "SHORT"
    entry_price: float,
    stop_price: float,
    target_price: float | None,
    spread_bps: float,
    data_age_seconds: float,
    account: AccountState,
    params: RiskParams,
    qty_step: float = 0.0,
    min_qty: float = 0.0,
    max_qty: float = float("inf"),
) -> RiskDecision:
    if side not in ("LONG", "SHORT"):
        return RiskDecision(False, "BAD_SIDE", f"side={side!r}")
    if account.equity_usd <= 0:
        return RiskDecision(False, "NO_EQUITY", _CODES["NO_EQUITY"])
    if stop_price <= 0 or entry_price <= 0:
        return RiskDecision(False, "NO_STOP", _CODES["NO_STOP"])
    if side == "LONG" and stop_price >= entry_price:
        return RiskDecision(False, "BAD_SIDE", "LONG with stop >= entry")
    if side == "SHORT" and stop_price <= entry_price:
        return RiskDecision(False, "BAD_SIDE", "SHORT with stop <= entry")
    if data_age_seconds > params.max_data_age_seconds:
        return RiskDecision(
            False,
            "STALE",
            f"data age {data_age_seconds:.1f}s > {params.max_data_age_seconds:.1f}s",
        )
    if spread_bps > params.max_spread_bps:
        return RiskDecision(
            False, "SPREAD", f"spread {spread_bps} bps > {params.max_spread_bps} bps"
        )
    if account.open_positions >= params.max_concurrent_positions:
        return RiskDecision(False, "MAX_POS", _CODES["MAX_POS"])
    if account.realised_pnl_today_usd < -params.max_daily_loss_frac * account.equity_usd:
        return RiskDecision(False, "DAILY_LOSS", _CODES["DAILY_LOSS"])
    if account.peak_equity_usd > 0:
        drawdown = 1.0 - account.equity_usd / account.peak_equity_usd
        if drawdown > params.max_drawdown_frac:
            return RiskDecision(
                False, "DRAWDOWN", f"drawdown {drawdown:.2%} > {params.max_drawdown_frac:.2%}"
            )

    if target_price is not None:
        rr = _rr(entry_price, stop_price, target_price, side)
        if rr < params.min_rr:
            return RiskDecision(False, "BAD_RR", f"RR {rr:.2f} < {params.min_rr:.2f}")

    size = size_position(
        equity_usd=account.equity_usd,
        risk_fraction=params.risk_per_trade,
        entry_price=entry_price,
        stop_price=stop_price,
        available_cash_usd=account.available_cash_usd,
        qty_step=qty_step,
        min_qty=min_qty,
        max_qty=max_qty,
        fee_rate=params.fee_rate,
        slippage_bps=params.slippage_bps,
    )
    if size.qty <= 0:
        return RiskDecision(False, "SIZE_ZERO", "; ".join(size.reasons), size)

    return RiskDecision(True, "OK", _CODES["OK"], size)


def require(decision: RiskDecision) -> SizeResult:
    """Raise `RiskRejection` if the decision was negative; return size otherwise."""
    if not decision.accepted:
        raise RiskRejection(decision.reason, code=decision.code)
    assert decision.size is not None
    return decision.size
