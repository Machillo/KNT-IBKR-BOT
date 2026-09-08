from __future__ import annotations

from dataclasses import dataclass
from math import sqrt

from market.history import PriceBar
from strategies.momentum import MomentumStrategy, SignalSide


@dataclass(frozen=True)
class BacktestTrade:
    side: str
    entry: float
    exit: float
    pnl: float
    return_pct: float
    reason: str


@dataclass(frozen=True)
class BacktestResult:
    initial_equity: float
    final_equity: float
    total_return_pct: float
    max_drawdown_pct: float
    trades: int
    wins: int
    losses: int
    win_rate_pct: float
    profit_factor: float | None
    sharpe: float | None
    trade_log: tuple[BacktestTrade, ...]


class BacktestEngine:
    """Long/short single-position simulator with stop/target and configurable costs."""

    def __init__(self, initial_equity: float = 10_000.0, risk_pct: float = 0.01,
                 commission_bps: float = 1.0, slippage_bps: float = 2.0) -> None:
        if initial_equity <= 0 or not 0 < risk_pct <= 0.10:
            raise ValueError("Invalid backtest capital/risk")
        self.initial_equity = initial_equity
        self.risk_pct = risk_pct
        self.cost_bps = commission_bps + slippage_bps

    def run(self, bars: list[PriceBar], strategy: MomentumStrategy) -> BacktestResult:
        equity = self.initial_equity
        peak = equity
        max_dd = 0.0
        trades: list[BacktestTrade] = []
        returns: list[float] = []
        warmup = max(strategy.slow, strategy.atr_period + 1) + 1

        position = None
        for i in range(warmup, len(bars)):
            bar = bars[i]
            if position is not None:
                side, entry, stop, target, qty = position
                exit_price = None
                reason = ""
                if side == SignalSide.LONG:
                    if bar.low <= stop:
                        exit_price, reason = stop, "stop"
                    elif bar.high >= target:
                        exit_price, reason = target, "target"
                else:
                    if bar.high >= stop:
                        exit_price, reason = stop, "stop"
                    elif bar.low <= target:
                        exit_price, reason = target, "target"
                if exit_price is not None:
                    gross = (exit_price - entry) * qty * (1 if side == SignalSide.LONG else -1)
                    costs = (entry + exit_price) * qty * self.cost_bps / 10_000
                    pnl = gross - costs
                    before = equity
                    equity += pnl
                    ret = pnl / before if before else 0.0
                    returns.append(ret)
                    trades.append(BacktestTrade(side.value, entry, exit_price, pnl, ret * 100, reason))
                    peak = max(peak, equity)
                    max_dd = max(max_dd, (peak - equity) / peak if peak else 0.0)
                    position = None

            if position is None:
                signal = strategy.evaluate(bars[:i + 1])
                if signal.side != SignalSide.FLAT and signal.entry and signal.stop and signal.target:
                    per_unit_risk = abs(signal.entry - signal.stop)
                    if per_unit_risk > 0:
                        qty = (equity * self.risk_pct) / per_unit_risk
                        position = (signal.side, signal.entry, signal.stop, signal.target, qty)

        if position is not None and bars:
            side, entry, _, _, qty = position
            exit_price = bars[-1].close
            gross = (exit_price - entry) * qty * (1 if side == SignalSide.LONG else -1)
            costs = (entry + exit_price) * qty * self.cost_bps / 10_000
            pnl = gross - costs
            before = equity
            equity += pnl
            ret = pnl / before if before else 0.0
            returns.append(ret)
            trades.append(BacktestTrade(side.value, entry, exit_price, pnl, ret * 100, "end_of_data"))
            peak = max(peak, equity)
            max_dd = max(max_dd, (peak - equity) / peak if peak else 0.0)

        wins = sum(t.pnl > 0 for t in trades)
        losses = sum(t.pnl <= 0 for t in trades)
        gains = sum(t.pnl for t in trades if t.pnl > 0)
        loss_abs = abs(sum(t.pnl for t in trades if t.pnl < 0))
        pf = gains / loss_abs if loss_abs > 0 else (None if gains == 0 else float("inf"))
        sharpe = None
        if len(returns) > 1:
            mean = sum(returns) / len(returns)
            variance = sum((x - mean) ** 2 for x in returns) / (len(returns) - 1)
            if variance > 0:
                sharpe = mean / sqrt(variance) * sqrt(len(returns))

        return BacktestResult(
            self.initial_equity, equity,
            (equity / self.initial_equity - 1) * 100,
            max_dd * 100, len(trades), wins, losses,
            wins / len(trades) * 100 if trades else 0.0,
            pf, sharpe, tuple(trades),
        )
