"""Offline backtest of the DECISION PIPELINE KNT would actually run.

Per symbol and completed bar:  RegimeDetector → StrategySelector (all strategies,
regime bonus/penalty, high-volatility pause, score threshold or NO_TRADE)
→ PortfolioAllocator-style sizing (risk % × volatility multiplier, notional cap,
integer shares) → portfolio admission (single-position cap, gross exposure,
cash reserve, correlation to open positions, daily-loss lock) → next-bar-open
entry with bracket stop/target, same fill/cost model as ``BacktestEngine``.

Shared capital across symbols, one position per symbol, events processed in
timestamp order. Only information available at each timestamp is used: signals
and regime use completed bars; correlations use returns up to the signal bar;
sizing uses equity marked at the last known closes.

HONEST LIMITATIONS (do not paper over them):
* Live discovery (IBKR scanners: most active / gainers / losers / hot by volume)
  cannot be reproduced historically from the cache. The universe here is a FIXED
  hand-picked cohort of current US names (survivorship bias). Results describe the
  selector on that cohort, not the discovery-driven system.
* Learning bonuses are off (no admissible OOS evidence exists yet), matching what
  the live selector would do today.
* Sector exposure is not modelled (sector data absent); correlation guard is.
"""
from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass, field, replace
from datetime import datetime
from math import floor, sqrt

from backtest.costs import BASELINE, CostModel
from backtest.metrics import as_datetime, cagr, daily_equity, period_returns, sharpe, sortino
from engine.strategy_selector import StrategySelector
from market.history import PriceBar
from strategies.momentum import SignalSide

CONTEXT_BARS = 450


@dataclass(frozen=True)
class PipelineConfig:
    name: str = "live_default"
    min_score: float = 55.0
    strategies: tuple[str, ...] | None = None      # None = every strategy in the selector
    allow_short: bool = False                       # live executor refuses shorts today
    risk_pct: float = 0.01                          # allocator: min(1 %, MAX_TRADE_RISK_PCT)
    use_volatility_multiplier: bool = True
    max_position_pct: float = 0.10                  # MAX_POSITION_PCT default
    max_gross_exposure_pct: float = 0.80            # PortfolioBrain default
    cash_reserve_pct: float = 0.05
    max_correlation: float = 0.85
    daily_loss_pct: float = 0.10                    # MAX_DAILY_LOSS_PCT default
    pause_high_volatility: bool = True
    regime_filter: tuple[str, ...] | None = None    # None = trade in any regime the selector allows
    cost_model: CostModel = BASELINE
    initial_equity: float = 100_000.0

    def variant(self, **changes) -> "PipelineConfig":
        return replace(self, **changes)


@dataclass(frozen=True)
class PipelineTrade:
    symbol: str
    strategy: str
    regime: str
    side: str
    entry_time: object
    exit_time: object
    entry: float
    exit: float
    quantity: float
    pnl: float
    return_pct: float
    reason: str
    costs: float


@dataclass(frozen=True)
class PipelineResult:
    config: str
    initial_equity: float
    final_equity: float
    total_return_pct: float
    cagr_pct: float | None
    max_drawdown_pct: float
    sharpe: float | None
    sortino: float | None
    trades: int
    win_rate_pct: float
    profit_factor: float | None
    expectancy: float
    exposure_pct: float
    total_costs: float
    decisions: int
    no_trade_decisions: int
    rejected_by_portfolio: int
    trade_log: tuple[PipelineTrade, ...] = ()
    equity_curve: tuple[tuple[object, float], ...] = ()
    monthly_returns_pct: tuple[float, ...] = ()


@dataclass
class _Open:
    symbol: str
    strategy: str
    regime: str
    side: SignalSide
    entry_time: object
    raw_entry: float
    entry_fill: float
    stop: float
    target: float
    qty: float
    entry_cost: float
    equity_before: float


@dataclass
class _Pending:
    symbol: str
    strategy: str
    regime: str
    side: SignalSide
    stop: float
    target: float
    score: float
    vol_multiplier: float
    returns: tuple[float, ...]


def _returns(bars: list[PriceBar], lookback: int = 60) -> tuple[float, ...]:
    closes = [b.close for b in bars[-(lookback + 1):] if b.close > 0]
    if len(closes) < lookback + 1:
        return ()
    return tuple(closes[i] / closes[i - 1] - 1 for i in range(1, len(closes)))


def _corr(a: tuple[float, ...], b: tuple[float, ...]) -> float | None:
    n = min(len(a), len(b))
    if n < 40:
        return None
    x, y = a[-n:], b[-n:]
    mx, my = sum(x) / n, sum(y) / n
    vx = sum((v - mx) ** 2 for v in x)
    vy = sum((v - my) ** 2 for v in y)
    if vx <= 0 or vy <= 0:
        return None
    return sum((i - mx) * (j - my) for i, j in zip(x, y)) / sqrt(vx * vy)


def _key(value) -> datetime:
    dt = as_datetime(value)
    if dt is None:
        raise ValueError(f"unparseable bar time: {value!r}")
    return dt


class PipelineBacktest:
    def __init__(self, data: dict[str, list[PriceBar]], config: PipelineConfig | None = None) -> None:
        self.config = config or PipelineConfig()
        self.data = {s: list(b) for s, b in data.items() if b}
        self.times = {s: [_key(b.time) for b in bars] for s, bars in self.data.items()}
        self.selector = StrategySelector(
            self.config.min_score, performance_store=None,
            pause_directional_high_volatility=self.config.pause_high_volatility,
        )
        if self.config.strategies is not None:
            wanted = set(self.config.strategies)
            self.selector.strategies = [s for s in self.selector.strategies if s.name in wanted]
            if not self.selector.strategies:
                raise ValueError("no strategies selected")

    def timeline(self) -> list[datetime]:
        return sorted({t for times in self.times.values() for t in times})

    def run(self, start: datetime | None = None, end: datetime | None = None) -> PipelineResult:
        """Simulate decisions whose ENTRY happens in [start, end); bars before ``start``
        are warmup context only; bars at or after ``end`` are never read."""
        cfg = self.config
        costs = cfg.cost_model
        events: list[tuple[datetime, str, int]] = []
        for symbol, times in self.times.items():
            for i, t in enumerate(times):
                if end is not None and t >= end:
                    break
                events.append((t, symbol, i))
        events.sort(key=lambda e: (e[0], e[1]))

        cash = cfg.initial_equity  # realized equity (entry/exit costs already applied)
        open_positions: dict[str, _Open] = {}
        pending: dict[str, _Pending] = {}
        last_close: dict[str, float] = {}
        trades: list[PipelineTrade] = []
        curve: list[tuple[object, float]] = []
        decisions = no_trade = rejected = 0
        in_market_ticks = total_ticks = 0
        day_key = None
        day_start_equity = cash
        locked_day = None
        active = start is None

        def marked() -> float:
            value = cash
            for pos in open_positions.values():
                px = last_close.get(pos.symbol, pos.entry_fill)
                direction = 1 if pos.side == SignalSide.LONG else -1
                value += (px - pos.entry_fill) * pos.qty * direction
                value -= costs.estimated_exit_cost(pos.qty, px, long=pos.side == SignalSide.LONG)
            return value

        def close_position(pos: _Open, raw_exit: float, reason: str, marketable: bool, t) -> None:
            nonlocal cash
            exit_fill = costs.marketable_fill(raw_exit, buy=(pos.side == SignalSide.SHORT)) if marketable else raw_exit
            commission = costs.commission(pos.qty, exit_fill)
            if pos.side == SignalSide.LONG:
                fee = costs.sell_fee(pos.qty, exit_fill)
            else:
                held = (t - pos.entry_time).total_seconds() / 86_400 if t is not None else 0.0
                fee = costs.borrow_cost(pos.entry_fill * pos.qty, held)
            direction = 1 if pos.side == SignalSide.LONG else -1
            gross = (exit_fill - pos.entry_fill) * pos.qty * direction
            cash += gross - commission - fee
            pnl = gross - pos.entry_cost - commission - fee
            spread = abs(exit_fill - raw_exit) * pos.qty + abs(pos.entry_fill - pos.raw_entry) * pos.qty
            trades.append(PipelineTrade(
                pos.symbol, pos.strategy, pos.regime, pos.side.value, pos.entry_time, t,
                pos.entry_fill, exit_fill, pos.qty, pnl, pnl / pos.equity_before * 100, reason,
                pos.entry_cost + commission + fee + spread,
            ))
            del open_positions[pos.symbol]

        for t, symbol, i in events:
            bars = self.data[symbol]
            bar = bars[i]
            if not active and start is not None and t >= start:
                active = True
                cash = cfg.initial_equity
            day = t.date()
            if day != day_key:
                day_key = day
                day_start_equity = marked()

            # 1) pending entry at this bar's open (only inside the active window)
            order = pending.pop(symbol, None)
            if order is not None and active and symbol not in open_positions and locked_day != day:
                raw = float(bar.open)
                valid = (order.stop < raw < order.target) if order.side == SignalSide.LONG else (order.target < raw < order.stop)
                if valid:
                    buy = order.side == SignalSide.LONG
                    fill = costs.marketable_fill(raw, buy=buy)
                    equity_now = marked()
                    per_unit = abs(fill - order.stop)
                    vm = max(0.10, min(order.vol_multiplier, 1.0)) if cfg.use_volatility_multiplier else 1.0
                    remaining_daily = max(0.0, day_start_equity * cfg.daily_loss_pct - max(0.0, day_start_equity - equity_now))
                    risk_budget = min(equity_now * cfg.risk_pct * vm, remaining_daily)
                    qty = 0.0
                    if per_unit > 0 and risk_budget > 0:
                        qty = floor(min(risk_budget / per_unit, equity_now * cfg.max_position_pct / fill) + 1e-9)
                    notional = qty * fill
                    gross = sum(p.entry_fill * p.qty for p in open_positions.values())
                    corr_ok = True
                    for pos in open_positions.values():
                        c = _corr(order.returns, _returns(self._bars_until(pos.symbol, t)))
                        if c is None or abs(c) > cfg.max_correlation:
                            corr_ok = False
                            break
                    if (qty < 1 or notional > equity_now * cfg.max_position_pct + 1e-6
                            or (gross + notional) > equity_now * cfg.max_gross_exposure_pct
                            or (cash - gross - notional) < equity_now * cfg.cash_reserve_pct
                            or not corr_ok):
                        rejected += 1
                    else:
                        commission = costs.commission(qty, fill)
                        fee = 0.0 if buy else costs.sell_fee(qty, fill)
                        cash -= commission + fee
                        open_positions[symbol] = _Open(
                            symbol, order.strategy, order.regime, order.side, t, raw, fill,
                            order.stop, order.target, qty, commission + fee, equity_now,
                        )

            # 2) exits for this symbol on this bar
            pos = open_positions.get(symbol)
            if pos is not None:
                if pos.side == SignalSide.LONG:
                    if bar.open <= pos.stop:
                        close_position(pos, float(bar.open), "stop_gap", True, t)
                    elif bar.low <= pos.stop:
                        close_position(pos, pos.stop, "stop", True, t)
                    elif bar.high >= pos.target:
                        close_position(pos, pos.target, "target", False, t)
                else:
                    if bar.open >= pos.stop:
                        close_position(pos, float(bar.open), "stop_gap", True, t)
                    elif bar.high >= pos.stop:
                        close_position(pos, pos.stop, "stop", True, t)
                    elif bar.low <= pos.target:
                        close_position(pos, pos.target, "target", False, t)
            last_close[symbol] = float(bar.close)

            # daily loss lock (sticky for the day), mirrors DailyLossGuard
            if active and day_start_equity > 0 and (day_start_equity - marked()) / day_start_equity >= cfg.daily_loss_pct:
                locked_day = day

            # 3) decision on the completed bar -> pending entry for this symbol's next bar
            times = self.times[symbol]
            if symbol not in open_positions and i + 1 < len(bars) and (end is None or times[i + 1] < end):
                if start is None or times[i + 1] >= start:
                    history = bars[max(0, i + 1 - CONTEXT_BARS):i + 1]
                    selection = self.selector.evaluate(history, symbol=symbol, timeframe="")
                    decisions += 1
                    chosen = selection.selected
                    regime = selection.regime.regime.value
                    tradable = chosen is not None and (cfg.allow_short or chosen.signal.side == SignalSide.LONG)
                    if tradable and cfg.regime_filter is not None and regime not in cfg.regime_filter:
                        tradable = False
                    if not tradable:
                        no_trade += 1
                    else:
                        sig = chosen.signal
                        stress = max(1.0, float(selection.regime.volatility_stress or 1.0))
                        pending[symbol] = _Pending(
                            symbol, chosen.strategy, regime, sig.side, float(sig.stop), float(sig.target),
                            chosen.adjusted_score, max(0.25, min(1.0, 1.0 / stress)), _returns(history),
                        )

            if active:
                total_ticks += 1
                in_market_ticks += bool(open_positions)
                curve.append((t, marked()))

        final_time = events[-1][0] if events else None
        for pos in list(open_positions.values()):
            close_position(pos, last_close.get(pos.symbol, pos.entry_fill), "end_of_data", True, final_time)
        if curve:
            curve.append((final_time, cash))
        return self._result(cash, trades, curve, decisions, no_trade, rejected, in_market_ticks, total_ticks)

    def _bars_until(self, symbol: str, t: datetime) -> list[PriceBar]:
        lo = bisect_left(self.times[symbol], t)
        return self.data[symbol][max(0, lo - 70):lo]

    def _result(self, equity, trades, curve, decisions, no_trade, rejected, in_market, total) -> PipelineResult:
        cfg = self.config
        peak = cfg.initial_equity
        max_dd = 0.0
        for _, value in curve:
            peak = max(peak, value)
            max_dd = max(max_dd, (peak - value) / peak if peak else 0.0)
        days = daily_equity(curve, cfg.initial_equity)
        daily = period_returns([v for _, v in days], cfg.initial_equity)
        months: dict[tuple[int, int], float] = {}
        for d, v in days:
            months[(d.year, d.month)] = v
        monthly = period_returns([months[k] for k in sorted(months)], cfg.initial_equity)
        wins = sum(t.pnl > 0 for t in trades)
        gains = sum(t.pnl for t in trades if t.pnl > 0)
        losses = abs(sum(t.pnl for t in trades if t.pnl < 0))
        growth = cagr(cfg.initial_equity, equity, days[0][0] if days else None, days[-1][0] if days else None)
        return PipelineResult(
            config=cfg.name,
            initial_equity=cfg.initial_equity,
            final_equity=equity,
            total_return_pct=(equity / cfg.initial_equity - 1) * 100,
            cagr_pct=None if growth is None else growth * 100,
            max_drawdown_pct=max_dd * 100,
            sharpe=sharpe(daily),
            sortino=sortino(daily),
            trades=len(trades),
            win_rate_pct=wins / len(trades) * 100 if trades else 0.0,
            profit_factor=(gains / losses) if losses > 0 else None,
            expectancy=sum(t.pnl for t in trades) / len(trades) if trades else 0.0,
            exposure_pct=in_market / total * 100 if total else 0.0,
            total_costs=sum(t.costs for t in trades),
            decisions=decisions,
            no_trade_decisions=no_trade,
            rejected_by_portfolio=rejected,
            trade_log=tuple(trades),
            equity_curve=tuple(curve),
            monthly_returns_pct=tuple(r * 100 for r in monthly),
        )


def time_split(timeline: list[datetime], train: float = 0.6, validation: float = 0.2) -> tuple[datetime, datetime]:
    """Boundaries (validation_start, holdout_start) on a shared timeline."""
    if len(timeline) < 10:
        raise ValueError("timeline too short")
    return timeline[int(len(timeline) * train)], timeline[int(len(timeline) * (train + validation))]


__all__ = ["PipelineBacktest", "PipelineConfig", "PipelineResult", "PipelineTrade", "time_split", "field"]
