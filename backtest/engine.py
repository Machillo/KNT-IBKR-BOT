from __future__ import annotations

from dataclasses import dataclass
from math import sqrt

from market.history import PriceBar
from strategies.momentum import SignalSide


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
    """Long/short single-position simulator with stop/target and configurable costs.

    Position size is bounded by both stop-risk and maximum notional exposure. This prevents
    unrealistically tight stops from creating implicit leverage and contaminating research
    evidence consumed by the Learning Engine.
    """

    def __init__(
        self,
        initial_equity: float = 10_000.0,
        risk_pct: float = 0.01,
        commission_bps: float = 1.0,
        slippage_bps: float = 2.0,
        max_position_pct: float = 0.25,
    ) -> None:
        if initial_equity <= 0 or not 0 < risk_pct <= 0.10:
            raise ValueError("Invalid backtest capital/risk")
        if not 0 < max_position_pct <= 1.0:
            raise ValueError("Invalid max_position_pct")
        self.initial_equity = initial_equity
        self.risk_pct = risk_pct
        self.max_position_pct = max_position_pct
        self.cost_bps = commission_bps + slippage_bps

    def _position_qty(self, *, equity: float, entry: float, stop: float) -> float:
        if equity <= 0 or entry <= 0:
            return 0.0
        per_unit_risk = abs(entry - stop)
        if per_unit_risk <= 0:
            return 0.0
        qty_by_risk = (equity * self.risk_pct) / per_unit_risk
        qty_by_notional = (equity * self.max_position_pct) / entry
        return max(0.0, min(qty_by_risk, qty_by_notional))

    def run(self, bars: list[PriceBar], strategy) -> BacktestResult:
        equity = self.initial_equity
        peak = equity
        max_dd = 0.0
        trades: list[BacktestTrade] = []
        returns: list[float] = []
        warmup = int(getattr(strategy, "warmup", 60))

        position = None
        for i in range(warmup, len(bars)):
            bar = bars[i]
            if position is not None:
                side, entry, stop, target, qty = position
                exit_price = None
                reason = ""
                # Conservative same-bar assumption: stop wins if both stop and target are touched.
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
                    qty = self._position_qty(equity=equity, entry=signal.entry, stop=signal.stop)
                    if qty > 0:
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
            m = sum(returns) / len(returns)
            variance = sum((x - m) ** 2 for x in returns) / (len(returns) - 1)
            if variance > 0:
                sharpe = m / sqrt(variance) * sqrt(len(returns))

        return BacktestResult(
            self.initial_equity, equity,
            (equity / self.initial_equity - 1) * 100,
            max_dd * 100, len(trades), wins, losses,
            wins / len(trades) * 100 if trades else 0.0,
            pf, sharpe, tuple(trades),
        )
