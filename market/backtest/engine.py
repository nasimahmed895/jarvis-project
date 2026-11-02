"""Bar-by-bar backtesting engine.

Design choices:

- A strategy is evaluated on the *closed* candles up to and including bar
  `i`, producing a decision usable from bar `i+1` onward. The engine NEVER
  looks past the current index. Decisions and fills always consume the next
  bar's open as the entry price. This avoids the common look-ahead where a
  strategy "entered on the signal bar's close".

- Fees are charged on both entry and exit.

- Slippage is applied as a fraction of the entry/exit price in the adverse
  direction. Configurable in bps.

- Stop-loss / take-profit resolution within one bar: if both the stop and
  the target lie inside the same OHLC range, the engine resolves ambiguity
  with a conservative assumption (stop first for LONG, stop first for SHORT).
  This is documented and labelled in the result so a reviewer can audit the
  assumption. If finer resolution is needed, re-run with lower-timeframe
  data.

- Reproducibility: engine inputs are `BacktestConfig`, candles, and the
  strategy identity + version. Same inputs yield identical outputs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..data.validator import closed_candles_only
from ..strategies.base import LONG_BIAS, NO_TRADE, SHORT_BIAS, Strategy, StrategyDecision
from ..types import Candle, OHLCVFrame


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    initial_balance_usd: float = 10_000.0
    fee_rate: float = 0.001           # 10 bps per side
    slippage_bps: float = 5.0
    sl_tp_resolution: str = "stop_first"   # conservative default
    # Fraction of equity staked per trade. Separate from the risk model —
    # backtests are deliberately simpler than live sizing to isolate the
    # strategy's signal from the risk engine's calibration.
    position_fraction: float = 0.10


@dataclass(frozen=True, slots=True)
class Trade:
    side: str           # "LONG" | "SHORT"
    entry_index: int
    entry_time_ms: int
    entry_price: float
    exit_index: int
    exit_time_ms: int
    exit_price: float
    qty: float
    gross_pnl_usd: float
    fees_usd: float
    slippage_usd: float
    net_pnl_usd: float
    exit_reason: str    # "stop" | "target" | "end_of_series"


@dataclass(frozen=True, slots=True)
class BacktestResult:
    config: BacktestConfig
    strategy_name: str
    strategy_version: str
    asset: str
    timeframe: str
    source: str
    bars: int
    trades: tuple[Trade, ...]
    final_equity_usd: float
    equity_curve: tuple[float, ...]
    assumptions: tuple[str, ...]

    @property
    def total_return(self) -> float:
        return self.final_equity_usd / self.config.initial_balance_usd - 1.0


def _apply_slippage(price: float, side: str, is_entry: bool, slip_bps: float) -> float:
    slip = price * (slip_bps / 10_000.0)
    if is_entry:
        return price + slip if side == "LONG" else price - slip
    return price - slip if side == "LONG" else price + slip


def _resolve_within_bar(
    bar: Candle, side: str, stop: float, target: float, resolution: str
) -> tuple[float, str] | None:
    hit_stop = bar.low <= stop <= bar.high if side == "LONG" else bar.low <= stop <= bar.high
    hit_target = bar.low <= target <= bar.high if side == "LONG" else bar.low <= target <= bar.high

    if side == "LONG":
        hit_stop = bar.low <= stop
        hit_target = bar.high >= target
    else:
        hit_stop = bar.high >= stop
        hit_target = bar.low <= target

    if hit_stop and hit_target:
        # Both reached inside one bar — order unknown. Documented conservative
        # fallback: assume the adverse exit happened first.
        if resolution == "stop_first":
            return stop, "stop"
        if resolution == "target_first":
            return target, "target"
        return stop, "stop"
    if hit_stop:
        return stop, "stop"
    if hit_target:
        return target, "target"
    return None


def run_backtest(
    frame: OHLCVFrame,
    strategy: Strategy,
    config: BacktestConfig,
) -> BacktestResult:
    """Replay a strategy over `frame`'s closed candles.

    Each iteration passes the strategy a frame that ends at bar `i`. If the
    strategy returns LONG_BIAS or SHORT_BIAS, the trade enters at `i+1`'s open.
    Exits happen on stop or target touch; otherwise the position closes at
    the final bar's close.
    """
    frame = closed_candles_only(frame)
    candles: list[Candle] = list(frame.candles)
    n = len(candles)

    initial = config.initial_balance_usd
    cash = initial
    equity_curve: list[float] = []
    trades: list[Trade] = []
    i = strategy.min_bars

    assumptions = (
        f"fees: {config.fee_rate * 10_000:.1f} bps per side (entry + exit charged)",
        f"slippage: {config.slippage_bps} bps per side",
        f"same-bar SL/TP: {config.sl_tp_resolution}",
        f"entry fill: next bar's open after a non-NO_TRADE decision",
        f"position fraction: {config.position_fraction:.0%} of equity per trade",
    )

    while i < n - 1:
        sub = OHLCVFrame(
            symbol=frame.symbol,
            timeframe=frame.timeframe,
            source=frame.source,
            retrieved_at_ms=candles[i].close_time_ms,
            candles=tuple(candles[: i + 1]),
        )
        decision: StrategyDecision = strategy.evaluate(sub, context={})
        if decision.decision == NO_TRADE or decision.stop_loss is None or not decision.targets:
            equity_curve.append(cash)
            i += 1
            continue

        side = "LONG" if decision.decision == LONG_BIAS else "SHORT"
        entry_bar = candles[i + 1]
        entry_price_raw = entry_bar.open
        entry_price = _apply_slippage(entry_price_raw, side, True, config.slippage_bps)
        stop = decision.stop_loss
        target = decision.targets[0]
        # Scale stop/target by the gap between the signal bar and the entry
        # bar open — strategy's absolute levels were based on the signal
        # bar's close.
        ref_close = candles[i].close
        shift = entry_price - ref_close
        stop_adj = stop + shift
        target_adj = target + shift

        notional = cash * config.position_fraction
        if notional <= 0:
            break
        qty = notional / entry_price
        exit_price = None
        exit_reason = "end_of_series"
        exit_index = n - 1
        for j in range(i + 1, n):
            bar = candles[j]
            resolution = _resolve_within_bar(bar, side, stop_adj, target_adj, config.sl_tp_resolution)
            if resolution is not None:
                px, reason = resolution
                exit_price = _apply_slippage(px, side, False, config.slippage_bps)
                exit_reason = reason
                exit_index = j
                break
        if exit_price is None:
            exit_price = _apply_slippage(candles[-1].close, side, False, config.slippage_bps)

        if side == "LONG":
            gross = (exit_price - entry_price) * qty
        else:
            gross = (entry_price - exit_price) * qty
        fees = (entry_price + exit_price) * qty * config.fee_rate
        slip_usd = 2.0 * qty * (abs(entry_price - entry_price_raw))
        net = gross - fees
        cash += net

        trades.append(
            Trade(
                side=side,
                entry_index=i + 1,
                entry_time_ms=entry_bar.open_time_ms,
                entry_price=entry_price,
                exit_index=exit_index,
                exit_time_ms=candles[exit_index].close_time_ms,
                exit_price=exit_price,
                qty=qty,
                gross_pnl_usd=gross,
                fees_usd=fees,
                slippage_usd=slip_usd,
                net_pnl_usd=net,
                exit_reason=exit_reason,
            )
        )
        equity_curve.append(cash)
        i = exit_index + 1

    while len(equity_curve) < n:
        equity_curve.append(cash)

    return BacktestResult(
        config=config,
        strategy_name=strategy.name,
        strategy_version=strategy.version,
        asset=frame.symbol,
        timeframe=frame.timeframe.value,
        source=frame.source,
        bars=n,
        trades=tuple(trades),
        final_equity_usd=cash,
        equity_curve=tuple(equity_curve),
        assumptions=assumptions,
    )
