"""Isolated simulated-trading account.

This engine will NEVER place a real order. There is no "live" code path.
Open/close operations go to the SQLite paper store; realised P/L updates
the paper account; the next tick advances positions against the latest
market price (passed in explicitly — the account does not fetch data on
its own).

Restart recovery:
- Positions already in the DB are reconstructed as-is.
- Duplicate orders are prevented by a unique `client_order_id` on the
  paper_positions table.
"""
from __future__ import annotations

import datetime as _dt
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from ..exceptions import MarketError
from .store import AccountRow, PaperStore, PositionRow, TradeRow


@dataclass
class OpenOrder:
    symbol: str
    side: str                # "LONG" | "SHORT"
    qty: float
    entry_price: float
    stop_price: float
    target_price: float | None
    strategy_name: str
    strategy_version: str
    client_order_id: str | None = None


@dataclass(frozen=True)
class ExecutionResult:
    accepted: bool
    reason: str
    client_order_id: str | None = None


class PaperAccount:
    """Simulated spot account. Spot-only — enforced by refusing short entries
    unless the configuration explicitly permits them (default: refuse).
    """

    def __init__(
        self,
        store: PaperStore,
        account_id: str = "default",
        initial_equity_usd: float = 10_000.0,
        fee_rate: float = 0.001,
        slippage_bps: float = 5.0,
        allow_short: bool = False,
    ):
        self._store = store
        self._id = account_id
        self._fee = fee_rate
        self._slip_bps = slippage_bps
        self._allow_short = allow_short
        self._state: AccountRow = store.get_or_init_account(
            account_id, initial_equity_usd, _today()
        )

    @property
    def state(self) -> AccountRow:
        return self._state

    @property
    def open_positions(self) -> list[PositionRow]:
        return self._store.list_positions(self._id)

    def _refresh_daily_pnl(self) -> None:
        today = _today()
        if self._state.pnl_reset_day != today:
            self._state = AccountRow(
                account_id=self._state.account_id,
                equity_usd=self._state.equity_usd,
                cash_usd=self._state.cash_usd,
                peak_equity_usd=self._state.peak_equity_usd,
                realised_pnl_today_usd=0.0,
                pnl_reset_day=today,
                updated_at_ms=int(time.time() * 1000),
            )
            self._store.update_account(self._state)

    def open_order(self, order: OpenOrder) -> ExecutionResult:
        self._refresh_daily_pnl()

        if order.side == "SHORT" and not self._allow_short:
            return ExecutionResult(False, "short selling disabled on spot v1")
        if order.qty <= 0:
            return ExecutionResult(False, "qty must be positive")

        notional = order.qty * order.entry_price
        fees = notional * self._fee
        if notional + fees > self._state.cash_usd:
            return ExecutionResult(False, "insufficient cash")

        coi = order.client_order_id or f"sim-{uuid.uuid4().hex[:16]}"
        pos = PositionRow(
            client_order_id=coi,
            account_id=self._id,
            symbol=order.symbol,
            side=order.side,
            qty=order.qty,
            entry_price=order.entry_price,
            stop_price=order.stop_price,
            target_price=order.target_price,
            opened_at_ms=int(time.time() * 1000),
            strategy_name=order.strategy_name,
            strategy_version=order.strategy_version,
        )
        if not self._store.insert_position(pos):
            return ExecutionResult(False, "duplicate client_order_id")

        self._state = AccountRow(
            account_id=self._state.account_id,
            equity_usd=self._state.equity_usd - fees,
            cash_usd=self._state.cash_usd - notional - fees,
            peak_equity_usd=max(self._state.peak_equity_usd, self._state.equity_usd - fees),
            realised_pnl_today_usd=self._state.realised_pnl_today_usd - fees,
            pnl_reset_day=self._state.pnl_reset_day,
            updated_at_ms=int(time.time() * 1000),
        )
        self._store.update_account(self._state)
        return ExecutionResult(True, "accepted", client_order_id=coi)

    def close_position(
        self, client_order_id: str, exit_price: float, exit_reason: str = "manual"
    ) -> ExecutionResult:
        self._refresh_daily_pnl()
        positions = {p.client_order_id: p for p in self.open_positions}
        pos = positions.get(client_order_id)
        if pos is None:
            return ExecutionResult(False, "position not found")

        if pos.side == "LONG":
            gross = (exit_price - pos.entry_price) * pos.qty
        else:
            gross = (pos.entry_price - exit_price) * pos.qty
        notional = pos.qty * exit_price
        fees = notional * self._fee
        net = gross - fees

        trade = TradeRow(
            trade_id=f"trd-{uuid.uuid4().hex[:16]}",
            client_order_id=client_order_id,
            account_id=self._id,
            symbol=pos.symbol,
            side=pos.side,
            qty=pos.qty,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            opened_at_ms=pos.opened_at_ms,
            closed_at_ms=int(time.time() * 1000),
            gross_pnl_usd=gross,
            fees_usd=fees,
            net_pnl_usd=net,
            exit_reason=exit_reason,
            strategy_name=pos.strategy_name,
            strategy_version=pos.strategy_version,
        )
        self._store.record_trade(trade)
        self._store.remove_position(client_order_id)

        new_cash = self._state.cash_usd + notional - fees + (pos.entry_price * pos.qty - notional)
        # Simpler: entry cash was already deducted when opening (notional + fees).
        # On close, we recover notional_exit - fees, and realise net.
        new_cash = self._state.cash_usd + (pos.entry_price * pos.qty) + net
        new_equity = self._state.equity_usd + net
        self._state = AccountRow(
            account_id=self._state.account_id,
            equity_usd=new_equity,
            cash_usd=new_cash,
            peak_equity_usd=max(self._state.peak_equity_usd, new_equity),
            realised_pnl_today_usd=self._state.realised_pnl_today_usd + net,
            pnl_reset_day=self._state.pnl_reset_day,
            updated_at_ms=int(time.time() * 1000),
        )
        self._store.update_account(self._state)
        return ExecutionResult(True, exit_reason, client_order_id=client_order_id)

    def mark_to_market(self, last_prices: dict[str, float]) -> None:
        """Trigger stop/target touches using the latest known prices.

        The caller supplies a dict {symbol: last_price}. The engine does NOT
        reach out to any provider. Hits are resolved conservatively (stop
        preferred over target when both are inside the same window). This
        is a simulation step, not a live order fill — labelled as such in
        the recorded exit_reason.
        """
        for pos in list(self.open_positions):
            px = last_prices.get(pos.symbol)
            if px is None:
                continue
            hit = None
            if pos.side == "LONG":
                if px <= pos.stop_price:
                    hit = ("stop", pos.stop_price)
                elif pos.target_price is not None and px >= pos.target_price:
                    hit = ("target", pos.target_price)
            else:
                if px >= pos.stop_price:
                    hit = ("stop", pos.stop_price)
                elif pos.target_price is not None and px <= pos.target_price:
                    hit = ("target", pos.target_price)
            if hit is not None:
                reason, exit_px = hit
                self.close_position(pos.client_order_id, exit_px, exit_reason=f"sim_{reason}")


def _today() -> str:
    return _dt.date.today().isoformat()
