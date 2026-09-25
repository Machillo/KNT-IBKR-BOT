from __future__ import annotations

from dataclasses import dataclass
from math import floor

from backtest.costs import BASELINE, CostModel
from backtest.metrics import (
    as_datetime, cagr, daily_equity, period_returns, sharpe as daily_sharpe, sortino as daily_sortino,
    trade_sharpe,
)
from market.history import PriceBar
from strategies.momentum import SignalSide

# Bumped whenever simulator semantics change materially. Research evidence produced
# by older engines is kept for history but is not admissible for live decisions.
# v1: stop filled at stop on gaps, bps-only costs, fractional qty, per-trade "Sharpe".
# v2: gap-aware stops, IBKR cost model, integer shares, daily Sharpe, trade_start windows.
ENGINE_VERSION = 2


@dataclass(frozen=True)
class BacktestTrade:
    side: str
    entry: float
    exit: float
    pnl: float
    return_pct: float
    reason: str
    quantity: float = 0.0
    commission: float = 0.0
    spread_slippage: float = 0.0
    entry_index: int = -1
    exit_index: int = -1


@dataclass(frozen=True)
class _OpenPosition:
    side: SignalSide
    raw_entry: float
    entry_fill: float
    stop: float
    target: float
    qty: float
    equity_before: float
    entry_cost: float
    entry_index: int


@dataclass(frozen=True)
class BacktestEquityPoint:
    time: object
    equity: float
    realized_equity: float
    drawdown_pct: float


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
    # Annualized Sharpe of DAILY mark-to-market returns (see backtest/metrics.py).
    sharpe: float | None
    trade_log: tuple[BacktestTrade, ...]
    equity_curve: tuple[BacktestEquityPoint, ...] = ()
    sortino: float | None = None
    trade_sharpe: float | None = None
    cagr_pct: float | None = None
    expectancy: float = 0.0
    total_commission: float = 0.0
    total_spread_slippage: float = 0.0
    exposure_pct: float = 0.0
    skipped_min_quantity: int = 0
    cost_model: str = ""


class BacktestEngine:
    """Long/short single-position simulator with explicit execution costs.

    Timing: a signal is evaluated on completed bars ``bars[:i+1]`` and may only
    execute at bar ``i+1``'s open. ``trade_start`` lets a window use earlier bars
    for indicator warmup while only entries at or after ``trade_start`` count;
    the equity curve and metrics also start there.

    Fills:
    * entry — marketable at the open, paying half spread + slippage;
    * stop — stop-market: if the bar OPENS beyond the stop (gap) the fill is the
      open, otherwise the stop; then half spread + slippage (adverse);
    * target — resting limit at the target price, no spread/slippage
      (conservative: a gap beyond the target is filled at the target, not the
      better open);
    * both stop and target inside one bar — the stop wins;
    * end of data — marketable at the last close.

    Quantity: rounded DOWN to ``quantity_step`` (1 share for stocks). If the risk
    budget cannot buy one step the setup is skipped and counted.
    """

    def __init__(
        self,
        initial_equity: float = 10_000.0,
        risk_pct: float = 0.01,
        commission_bps: float | None = None,
        slippage_bps: float | None = None,
        max_position_pct: float = 0.25,
        *,
        cost_model: CostModel | None = None,
        quantity_step: float | None = 1.0,
        allow_short: bool = True,
    ) -> None:
        if initial_equity <= 0 or not 0 < risk_pct <= 0.10:
            raise ValueError("Invalid backtest capital/risk")
        if not 0 < max_position_pct <= 1.0:
            raise ValueError("Invalid max_position_pct")
        if quantity_step is not None and quantity_step <= 0:
            raise ValueError("quantity_step must be > 0 or None")
        if cost_model is not None and (commission_bps is not None or slippage_bps is not None):
            raise ValueError("Pass either cost_model or legacy bps arguments, not both")
        if cost_model is None:
            if commission_bps is not None or slippage_bps is not None:
                cost_model = CostModel.legacy_bps(commission_bps or 0.0, slippage_bps or 0.0)
            else:
                cost_model = BASELINE
        self.initial_equity = float(initial_equity)
        self.risk_pct = float(risk_pct)
        self.max_position_pct = float(max_position_pct)
        self.costs = cost_model
        self.quantity_step = quantity_step
        self.allow_short = bool(allow_short)

    def _position_qty(self, *, equity: float, entry: float, stop: float) -> float:
        if equity <= 0 or entry <= 0:
            return 0.0
        per_unit_risk = abs(entry - stop)
        if per_unit_risk <= 0:
            return 0.0
        qty_by_risk = (equity * self.risk_pct) / per_unit_risk
        qty_by_notional = (equity * self.max_position_pct) / entry
        qty = max(0.0, min(qty_by_risk, qty_by_notional))
        if self.quantity_step is not None:
            qty = floor(qty / self.quantity_step + 1e-9) * self.quantity_step
        return qty

    @staticmethod
    def _entry_is_valid(side: SignalSide, entry: float, stop: float, target: float) -> bool:
        if min(entry, stop, target) <= 0:
            return False
        if side == SignalSide.LONG:
            return stop < entry < target
        if side == SignalSide.SHORT:
            return target < entry < stop
        return False

    def _exit_fill(self, side: SignalSide, raw: float, *, marketable: bool) -> float:
        if not marketable:
            return raw
        # Closing a long sells; closing a short buys.
        return self.costs.marketable_fill(raw, buy=(side == SignalSide.SHORT))

    def run(self, bars: list[PriceBar], strategy, trade_start: int = 0) -> BacktestResult:
        costs = self.costs
        equity = self.initial_equity
        peak_marked = equity
        max_dd = 0.0
        trades: list[BacktestTrade] = []
        trade_returns: list[float] = []
        curve: list[BacktestEquityPoint] = []
        warmup = int(getattr(strategy, "warmup", 60))
        start = max(0, int(trade_start))
        total_commission = 0.0
        total_spread_slip = 0.0
        skipped_min_qty = 0
        bars_in_market = 0
        bars_counted = 0

        position: _OpenPosition | None = None
        pending_signal = None
        first_eval = max(warmup - 1, start - 1, 0)
        for i in range(first_eval, len(bars)):
            bar = bars[i]

            if position is None and pending_signal is not None and i >= start:
                signal = pending_signal
                pending_signal = None
                raw_entry = float(bar.open)
                stop = float(signal.stop)
                target = float(signal.target)
                if self._entry_is_valid(signal.side, raw_entry, stop, target):
                    buy = signal.side == SignalSide.LONG
                    entry_fill = costs.marketable_fill(raw_entry, buy=buy)
                    if self._entry_is_valid(signal.side, entry_fill, stop, target):
                        qty = self._position_qty(equity=equity, entry=entry_fill, stop=stop)
                        if qty > 0:
                            commission = costs.commission(qty, entry_fill)
                            fee = 0.0 if buy else costs.sell_fee(qty, entry_fill)
                            spread_cost = abs(entry_fill - raw_entry) * qty
                            equity_before = equity
                            equity -= commission + fee
                            total_commission += commission + fee
                            total_spread_slip += spread_cost
                            position = _OpenPosition(signal.side, raw_entry, entry_fill, stop, target,
                                                     qty, equity_before, commission + fee, i)
                        else:
                            skipped_min_qty += 1
            elif pending_signal is not None and i < start:
                pending_signal = None

            if position is not None:
                pos = position
                raw_exit = None
                reason = ""
                marketable = True
                if pos.side == SignalSide.LONG:
                    if bar.open <= pos.stop:
                        raw_exit, reason = float(bar.open), "stop_gap"
                    elif bar.low <= pos.stop:
                        raw_exit, reason = pos.stop, "stop"
                    elif bar.high >= pos.target:
                        raw_exit, reason, marketable = pos.target, "target", False
                else:
                    if bar.open >= pos.stop:
                        raw_exit, reason = float(bar.open), "stop_gap"
                    elif bar.high >= pos.stop:
                        raw_exit, reason = pos.stop, "stop"
                    elif bar.low <= pos.target:
                        raw_exit, reason, marketable = pos.target, "target", False
                if raw_exit is not None:
                    trade, equity_delta, exit_costs, exit_spread = self._close(pos, raw_exit, reason, marketable, i)
                    trades.append(trade)
                    trade_returns.append(trade.return_pct / 100)
                    equity += equity_delta
                    total_commission += exit_costs
                    total_spread_slip += exit_spread
                    position = None

            if position is None and i + 1 < len(bars) and i + 1 >= start and i + 1 >= warmup:
                signal = strategy.evaluate(bars[:i + 1])
                if signal.side != SignalSide.FLAT and signal.entry and signal.stop and signal.target:
                    if signal.side == SignalSide.LONG or self.allow_short:
                        pending_signal = signal

            if i < start:
                continue
            bars_counted += 1
            marked = equity
            if position is not None:
                bars_in_market += 1
                direction = 1 if position.side == SignalSide.LONG else -1
                close = float(bar.close)
                marked = (equity + (close - position.entry_fill) * position.qty * direction
                          - costs.commission(position.qty, close))
            peak_marked = max(peak_marked, marked)
            drawdown = (peak_marked - marked) / peak_marked if peak_marked else 0.0
            max_dd = max(max_dd, drawdown)
            curve.append(BacktestEquityPoint(bar.time, marked, equity, drawdown * 100))

        if position is not None and bars:
            trade, equity_delta, exit_costs, exit_spread = self._close(
                position, float(bars[-1].close), "end_of_data", True, len(bars) - 1)
            trades.append(trade)
            trade_returns.append(trade.return_pct / 100)
            equity += equity_delta
            total_commission += exit_costs
            total_spread_slip += exit_spread
            peak_marked = max(peak_marked, equity)
            drawdown = (peak_marked - equity) / peak_marked if peak_marked else 0.0
            max_dd = max(max_dd, drawdown)
            curve.append(BacktestEquityPoint(bars[-1].time, equity, equity, drawdown * 100))

        return self._result(equity, max_dd, trades, trade_returns, curve, total_commission,
                            total_spread_slip, bars_in_market, bars_counted, skipped_min_qty)

    def _close(self, pos: _OpenPosition, raw_exit: float, reason: str, marketable: bool,
               index: int) -> tuple[BacktestTrade, float, float, float]:
        """Return (trade, equity change at exit, exit commission+fees, exit spread/slippage)."""
        exit_fill = self._exit_fill(pos.side, raw_exit, marketable=marketable)
        commission = self.costs.commission(pos.qty, exit_fill)
        fee = self.costs.sell_fee(pos.qty, exit_fill) if pos.side == SignalSide.LONG else 0.0
        direction = 1 if pos.side == SignalSide.LONG else -1
        gross = (exit_fill - pos.entry_fill) * pos.qty * direction
        pnl = gross - pos.entry_cost - commission - fee
        ret = pnl / pos.equity_before if pos.equity_before else 0.0
        exit_spread = abs(exit_fill - raw_exit) * pos.qty
        entry_spread = abs(pos.entry_fill - pos.raw_entry) * pos.qty
        trade = BacktestTrade(
            pos.side.value, pos.entry_fill, exit_fill, pnl, ret * 100, reason, pos.qty,
            pos.entry_cost + commission + fee, entry_spread + exit_spread, pos.entry_index, index,
        )
        return trade, gross - commission - fee, commission + fee, exit_spread

    def _result(self, equity, max_dd, trades, trade_returns, curve, total_commission,
                total_spread_slip, bars_in_market, bars_counted, skipped_min_qty) -> BacktestResult:
        wins = sum(t.pnl > 0 for t in trades)
        losses = sum(t.pnl <= 0 for t in trades)
        gains = sum(t.pnl for t in trades if t.pnl > 0)
        loss_abs = abs(sum(t.pnl for t in trades if t.pnl < 0))
        pf = gains / loss_abs if loss_abs > 0 else (None if gains == 0 else float("inf"))

        days = daily_equity(((p.time, p.equity) for p in curve), self.initial_equity)
        daily = period_returns([v for _, v in days], self.initial_equity)
        growth = cagr(self.initial_equity, equity, days[0][0] if days else None, days[-1][0] if days else None)

        return BacktestResult(
            initial_equity=self.initial_equity,
            final_equity=equity,
            total_return_pct=(equity / self.initial_equity - 1) * 100,
            max_drawdown_pct=max_dd * 100,
            trades=len(trades),
            wins=wins,
            losses=losses,
            win_rate_pct=wins / len(trades) * 100 if trades else 0.0,
            profit_factor=pf,
            sharpe=daily_sharpe(daily),
            trade_log=tuple(trades),
            equity_curve=tuple(curve),
            sortino=daily_sortino(daily),
            trade_sharpe=trade_sharpe(trade_returns),
            cagr_pct=None if growth is None else growth * 100,
            expectancy=(sum(t.pnl for t in trades) / len(trades)) if trades else 0.0,
            total_commission=total_commission,
            total_spread_slippage=total_spread_slip,
            exposure_pct=(bars_in_market / bars_counted * 100) if bars_counted else 0.0,
            skipped_min_quantity=skipped_min_qty,
            cost_model=self.costs.name,
        )


__all__ = ["BacktestEngine", "BacktestResult", "BacktestTrade", "BacktestEquityPoint", "as_datetime"]
