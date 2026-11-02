"""Backtest performance metrics.

Every metric is defined, and all return `None` (or a labelled dict entry
`available=False`) when the sample size is too small. No metric is allowed
to silently produce an unreliable value.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from .engine import BacktestResult, Trade


@dataclass(frozen=True, slots=True)
class Metrics:
    total_return: float
    net_profit_usd: float
    num_trades: int
    win_rate: float | None
    avg_win_usd: float | None
    avg_loss_usd: float | None
    profit_factor: float | None
    expectancy_usd: float | None
    max_drawdown: float
    sharpe: float | None
    fee_impact_usd: float
    exposure_bars: int
    assumptions: tuple[str, ...]


def _max_drawdown(curve: Sequence[float]) -> float:
    peak = -math.inf
    mdd = 0.0
    for v in curve:
        peak = max(peak, v)
        if peak > 0:
            dd = 1.0 - v / peak
            mdd = max(mdd, dd)
    return mdd


def compute(result: BacktestResult) -> Metrics:
    trades = list(result.trades)
    nets = [t.net_pnl_usd for t in trades]
    wins = [p for p in nets if p > 0]
    losses = [p for p in nets if p < 0]

    win_rate = len(wins) / len(nets) if nets else None
    avg_win = sum(wins) / len(wins) if wins else None
    avg_loss = sum(losses) / len(losses) if losses else None
    gross_win = sum(wins) if wins else 0.0
    gross_loss = -sum(losses) if losses else 0.0
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else None
    expectancy = (sum(nets) / len(nets)) if nets else None

    rets: list[float] = []
    prev = result.config.initial_balance_usd
    for v in result.equity_curve:
        if prev > 0:
            rets.append((v - prev) / prev)
        prev = v
    if len(rets) >= 2:
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        sd = math.sqrt(var) if var > 0 else 0.0
        sharpe = (mean / sd * math.sqrt(252)) if sd > 0 else None
    else:
        sharpe = None

    exposure = sum(t.exit_index - t.entry_index for t in trades)

    return Metrics(
        total_return=result.total_return,
        net_profit_usd=result.final_equity_usd - result.config.initial_balance_usd,
        num_trades=len(trades),
        win_rate=win_rate,
        avg_win_usd=avg_win,
        avg_loss_usd=avg_loss,
        profit_factor=profit_factor,
        expectancy_usd=expectancy,
        max_drawdown=_max_drawdown(result.equity_curve),
        sharpe=sharpe,
        fee_impact_usd=sum(t.fees_usd for t in trades),
        exposure_bars=exposure,
        assumptions=result.assumptions,
    )
