"""Position sizing for simplified spot positions.

Deliberately spot-only. For leveraged derivatives, a dedicated margin and
liquidation model is required and NOT provided here.

Formula:
    risk_budget_usd = account_equity_usd * risk_fraction
    quantity        = risk_budget_usd / abs(entry_price - stop_price)

Then applied constraints:
    - quantity_precision     (floor to the exchange's step size)
    - min_order_qty, max_order_qty
    - available_cash         (ensure notional fits)
    - fee + slippage reduce effective realised risk below the budget

A failed constraint returns `qty=0.0` and a reason — the risk gate uses this
to reject the trade.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SizeResult:
    qty: float
    notional_usd: float
    risk_budget_usd: float
    reasons: tuple[str, ...]


def floor_to_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    return math.floor(value / step) * step


def size_position(
    *,
    equity_usd: float,
    risk_fraction: float,
    entry_price: float,
    stop_price: float,
    available_cash_usd: float,
    qty_step: float = 0.0,
    min_qty: float = 0.0,
    max_qty: float = float("inf"),
    fee_rate: float = 0.0,
    slippage_bps: float = 0.0,
) -> SizeResult:
    reasons: list[str] = []
    if equity_usd <= 0:
        return SizeResult(0.0, 0.0, 0.0, ("non-positive equity",))
    if not (0 < risk_fraction <= 0.1):
        return SizeResult(0.0, 0.0, 0.0, ("risk_fraction out of (0, 0.1]",))
    if entry_price <= 0 or stop_price <= 0:
        return SizeResult(0.0, 0.0, 0.0, ("non-positive prices",))
    distance = abs(entry_price - stop_price)
    if distance <= 0:
        return SizeResult(0.0, 0.0, 0.0, ("entry equals stop",))

    risk_budget = equity_usd * risk_fraction

    # Account for slippage: expected realised stop distance is wider by the
    # slippage. We size against the wider distance so the realised loss stays
    # within budget.
    slip_frac = slippage_bps / 10_000.0
    effective_distance = distance * (1.0 + slip_frac)
    qty = risk_budget / effective_distance

    if qty_step > 0:
        qty = floor_to_step(qty, qty_step)
    if qty < min_qty:
        return SizeResult(0.0, 0.0, risk_budget, (f"qty {qty} below min_qty {min_qty}",))
    if qty > max_qty:
        qty = max_qty
        reasons.append(f"qty capped at max_qty {max_qty}")

    notional = qty * entry_price
    fee_cost = notional * fee_rate * 2.0  # entry + exit fee
    if notional + fee_cost > available_cash_usd:
        return SizeResult(
            0.0,
            notional,
            risk_budget,
            (f"notional {notional:.2f}+fees {fee_cost:.2f} > available cash {available_cash_usd:.2f}",),
        )

    return SizeResult(qty=qty, notional_usd=notional, risk_budget_usd=risk_budget, reasons=tuple(reasons))
