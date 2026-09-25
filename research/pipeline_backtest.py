"""Offline replay of the decision KNT would actually take (pipeline v2).

Per symbol and completed bar the replay calls the SAME ``engine.decision.DecisionPipeline``
used by the shadow/paper runtime — real ``StrategySelector``, ``PortfolioAllocator`` and
``PortfolioAdmissionCoordinator`` (RiskManager + PortfolioBrain + CrossExposureGuard) —
against a simulated ``PortfolioState``. Only execution is simulated, mirroring the
paper executor:

* entry = LIMIT at the (tick-normalized) signal price, DAY validity: fills at the next
  bars of that exchange date if price trades through the limit (an open below the limit
  fills at the open), otherwise expires;
* bracket children: gap-aware stop-market and limit target, stop wins ties;
* long-only unless ``allow_short`` (the executor refuses shorts today);
* daily entry cap (executor default 3 transmissions/day) and daily-loss lock;
* only symbols in the point-in-time universe at decision time may be decided.

Costs: ``backtest.costs.CostModel`` (IBKR fixed commission, spread, slippage, sell fee,
borrow). Only information available at each timestamp is used.

HONEST LIMITATIONS
* With ``StaticCohortUniverse`` the universe is a hand-picked, survivorship-biased
  cohort; historical scanner discovery is not reproduced. ``JournalUniverse`` replays
  what KNT's scanners actually returned, but only from when shadow started recording.
* Learning bonuses are off (no performance store), matching today's operational state
  without admissible evidence.
* Sector metadata is absent in the cache; the sector guard sees UNKNOWN.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from itertools import groupby
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from math import floor
from zoneinfo import ZoneInfo

from backtest.costs import BASELINE, CostModel
from backtest.metrics import as_datetime, cagr, daily_equity, period_returns, sharpe, sortino
from config.config import RiskConfig
from engine.decision import DecisionPipeline, exposure_symbols, return_series
from engine.strategy_selector import StrategySelector
from market.history import PriceBar
from portfolio.admission import PortfolioAdmissionCoordinator
from portfolio.allocation import PortfolioAllocator
from portfolio.brain import PortfolioBrain, PortfolioSnapshot
from portfolio.state import PendingOrderExposure, PortfolioState, PositionExposure
from risk.risk_manager import RiskManager
from strategies.momentum import SignalSide

_EXCHANGE_TZ = ZoneInfo("America/New_York")

CONTEXT_BARS = 450
PIPELINE_VERSION = 3  # v3: quant-review fixes (aligned correlations, strict limit fills, score priority)


@dataclass(frozen=True)
class PipelineConfig:
    name: str = "live_default"
    min_score: float = 55.0
    strategies: tuple[str, ...] | None = None      # None = every strategy in the selector
    allow_short: bool = False                       # live executor refuses shorts today
    risk_pct: float = 0.01                          # allocator: min(1 %, MAX_TRADE_RISK_PCT)
    use_volatility_multiplier: bool = True
    max_trade_risk_pct: float = 0.10                # RiskConfig default (hard ceiling)
    max_position_pct: float = 0.10                  # MAX_POSITION_PCT default
    max_gross_exposure_pct: float = 0.80            # PortfolioBrain default
    cash_reserve_pct: float = 0.05
    max_correlation: float = 0.85
    daily_loss_pct: float = 0.10                    # MAX_DAILY_LOSS_PCT default
    max_entries_per_day: int = 3                    # PaperExecutionEngine default
    entry_mode: str = "limit_day"                   # "limit_day" (as executor) | "next_open" (legacy v1)
    pause_high_volatility: bool = True
    regime_filter: tuple[str, ...] | None = None    # research-only filters below
    symbol_trend_sma: int | None = None
    market_filter: tuple[str, int] | None = None
    bracket_atr: tuple[float, float] | None = None
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
    submitted: int = 0
    unfilled_expired: int = 0
    blocked_by_cap_or_lock: int = 0
    outside_universe: int = 0
    blocked_session_closed: int = 0
    censored_at_end: int = 0
    pipeline_version: int = PIPELINE_VERSION


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
    filled_at_open: bool = True


@dataclass
class _Order:
    symbol: str
    strategy: str
    regime: str
    side: SignalSide
    limit: float
    stop: float
    target: float
    qty: float
    expiry: date


def _returns(bars: list[PriceBar], lookback: int = 60) -> tuple[float, ...]:
    return return_series(bars, lookback)


def _key(value) -> datetime:
    """Exchange wall-clock time (America/New_York) without tzinfo, so calendar protocol
    boundaries compare uniformly across profiles. Aware times are CONVERTED first: bars fetched
    with ``complete_only`` carry UTC offsets, and stripping those without converting would
    shift every bar 4-5 h into the future (point-in-time universe look-ahead)."""
    dt = as_datetime(value)
    if dt is None:
        raise ValueError(f"unparseable bar time: {value!r}")
    if dt.tzinfo is not None:
        dt = dt.astimezone(_EXCHANGE_TZ)
    return dt.replace(tzinfo=None)


def _tick(price: float) -> float:
    return float(Decimal(str(price)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


class _FixedSizeAllocator:
    """Research-only wrapper: ignore the volatility multiplier (H6)."""

    def __init__(self, inner: PortfolioAllocator) -> None:
        self.inner = inner

    def propose(self, snapshot, **kwargs):
        kwargs["volatility_multiplier"] = 1.0
        return self.inner.propose(snapshot, **kwargs)


class PipelineBacktest:
    def __init__(self, data: dict[str, list[PriceBar]], config: PipelineConfig | None = None,
                 universe=None) -> None:
        self.config = cfg = config or PipelineConfig()
        self.data = {s: list(b) for s, b in data.items() if b}
        self.times = {s: [_key(b.time) for b in bars] for s, bars in self.data.items()}
        # Intraday data = some consecutive bars share a date. Intraday orders decided after the
        # session's last bar are refused (the executor's session policy is closed then).
        self.intraday = {s: any(a.date() == b.date() for a, b in zip(ts, ts[1:])) for s, ts in self.times.items()}
        self.universe = universe
        self.selector = StrategySelector(
            cfg.min_score, performance_store=None,
            pause_directional_high_volatility=cfg.pause_high_volatility,
        )
        if cfg.strategies is not None:
            wanted = set(cfg.strategies)
            self.selector.strategies = [s for s in self.selector.strategies if s.name in wanted]
            if not self.selector.strategies:
                raise ValueError("no strategies selected")
        self.risk = RiskManager(RiskConfig(
            max_trade_risk_pct=cfg.max_trade_risk_pct, max_daily_loss_pct=cfg.daily_loss_pct,
            max_position_pct=cfg.max_position_pct, kill_switch_enabled=True, kill_switch_dry_run=True,
        ))
        allocator = PortfolioAllocator(risk_pct=min(cfg.risk_pct, cfg.max_trade_risk_pct),
                                       max_position_pct=cfg.max_position_pct)
        self.allocator = allocator if cfg.use_volatility_multiplier else _FixedSizeAllocator(allocator)
        self.admission = PortfolioAdmissionCoordinator(
            self.risk,
            portfolio_brain=PortfolioBrain(
                max_gross_exposure_pct=cfg.max_gross_exposure_pct,
                max_single_position_pct=cfg.max_position_pct,
                max_correlation=cfg.max_correlation, cash_reserve_pct=cfg.cash_reserve_pct,
            ),
        )
        # The SAME decision object type the runtime uses (engine/shadow.py).
        self.pipeline = DecisionPipeline(self.selector, self.allocator, self.admission, allow_short=cfg.allow_short)

    def timeline(self) -> list[datetime]:
        return sorted({t for times in self.times.values() for t in times})

    # ------------------------------------------------------------------ helpers
    def _bars_until(self, symbol: str, t: datetime, lookback: int = 70) -> list[PriceBar]:
        """Bars of ``symbol`` strictly before ``t`` (all completed when deciding at ``t``)."""
        lo = bisect_left(self.times[symbol], t)
        return self.data[symbol][max(0, lo - lookback):lo]

    def _bars_through(self, symbol: str, t: datetime, lookback: int = 70) -> list[PriceBar]:
        """Bars of ``symbol`` up to and including the bar stamped ``t``.

        Bars sharing a timestamp complete together, so at a decision on the bar stamped
        ``t`` every other symbol's bar ``t`` is complete too. Using it keeps correlation
        inputs aligned with the candidate's own series (same bars, no lag)."""
        hi = bisect_right(self.times[symbol], t)
        return self.data[symbol][max(0, hi - lookback):hi]

    def _filters_allow(self, side: SignalSide, history: list[PriceBar], t: datetime) -> bool:
        cfg = self.config
        if cfg.symbol_trend_sma is not None:
            n = cfg.symbol_trend_sma
            if len(history) < n:
                return False
            above = history[-1].close > sum(b.close for b in history[-n:]) / n
            if (side == SignalSide.LONG and not above) or (side == SignalSide.SHORT and above):
                return False
        if cfg.market_filter is not None:
            market_symbol, n = cfg.market_filter
            if market_symbol not in self.data:
                return False  # fail closed: no market data, no trade
            mkt = self._bars_until(market_symbol, t, lookback=n)
            if len(mkt) < n:
                return False
            above = mkt[-1].close > sum(b.close for b in mkt) / n
            if (side == SignalSide.LONG and not above) or (side == SignalSide.SHORT and above):
                return False
        return True

    @staticmethod
    def _atr_bracket(side: SignalSide, history: list[PriceBar], stop_mult: float,
                     target_mult: float) -> tuple[float, float]:
        sample = history[-15:]
        trs = [max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close)) for p, c in zip(sample, sample[1:])]
        a = sum(trs) / len(trs) if trs else 0.0
        price = history[-1].close
        if side == SignalSide.LONG:
            return max(0.01, price - stop_mult * a), price + target_mult * a
        return price + stop_mult * a, max(0.01, price - target_mult * a)

    # --------------------------------------------------------------------- run
    def run(self, start: datetime | None = None, end: datetime | None = None) -> PipelineResult:
        """Replay decisions whose ENTRY happens in [start, end); earlier bars are warmup
        context only; bars at or after ``end`` are never read.

        Each timestamp is processed like a runtime cycle: first working orders and exits for
        every symbol with a bar at ``t``, then decisions on the completed bars ordered by the
        selector's adjusted score (highest first), so capacity and the daily entry cap go to
        the strongest setups rather than to alphabetical order.
        """
        cfg = self.config
        costs = cfg.cost_model
        events: list[tuple[datetime, str, int]] = []
        for symbol, times in self.times.items():
            for i, t in enumerate(times):
                if end is not None and t >= end:
                    break
                events.append((t, symbol, i))
        events.sort(key=lambda e: (e[0], e[1]))

        self._realized = cfg.initial_equity
        open_positions: dict[str, _Open] = {}
        orders: dict[str, _Order] = {}
        last_close: dict[str, float] = {}
        trades: list[PipelineTrade] = []
        curve: list[tuple[object, float]] = []
        stats = dict(decisions=0, no_trade=0, rejected=0, submitted=0, expired=0, blocked=0, outside=0,
                     session_closed=0, censored=0)
        in_market = total = 0
        day_key = None
        day_start_equity = self._realized
        entries_today = 0
        active = start is None

        def marked() -> float:
            value = self._realized
            for pos in open_positions.values():
                px = last_close.get(pos.symbol, pos.entry_fill)
                direction = 1 if pos.side == SignalSide.LONG else -1
                value += (px - pos.entry_fill) * pos.qty * direction
                value -= costs.estimated_exit_cost(pos.qty, px, long=pos.side == SignalSide.LONG)
            return value

        def cash() -> float:
            spent = sum(p.entry_fill * p.qty for p in open_positions.values() if p.side == SignalSide.LONG)
            return self._realized - spent

        def state(t: datetime) -> PortfolioState:
            equity = marked()
            positions = tuple(
                PositionExposure(p.symbol, "STK", p.qty if p.side == SignalSide.LONG else -p.qty,
                                 last_close.get(p.symbol, p.entry_fill),
                                 abs(p.qty * last_close.get(p.symbol, p.entry_fill)))
                for p in open_positions.values()
            )
            pending = tuple(PendingOrderExposure(o.symbol, "STK", o.qty, o.limit, o.qty * o.limit)
                            for o in orders.values())
            snapshot = PortfolioSnapshot(
                net_liquidation=equity, cash=max(0.0, cash()),
                committed_notional=sum(p.notional for p in positions), open_position_risk=0.0,
                pending_order_notional=sum(o.notional for o in pending),
                daily_loss_used=max(0.0, day_start_equity - equity),
                daily_loss_limit=day_start_equity * cfg.daily_loss_pct,
                trading_locked=self.risk.trading_locked,
            )
            return PortfolioState(snapshot, positions, pending)

        def close_position(pos: _Open, raw_exit: float, reason: str, marketable: bool, t) -> None:
            exit_fill = costs.marketable_fill(raw_exit, buy=(pos.side == SignalSide.SHORT)) if marketable else raw_exit
            commission = costs.commission(pos.qty, exit_fill)
            if pos.side == SignalSide.LONG:
                fee = costs.sell_fee(pos.qty, exit_fill)
            else:
                held = (t - pos.entry_time).total_seconds() / 86_400 if t is not None else 0.0
                fee = costs.borrow_cost(pos.entry_fill * pos.qty, held)
            direction = 1 if pos.side == SignalSide.LONG else -1
            gross = (exit_fill - pos.entry_fill) * pos.qty * direction
            self._realized += gross - commission - fee
            pnl = gross - pos.entry_cost - commission - fee
            spread = abs(exit_fill - raw_exit) * pos.qty + abs(pos.entry_fill - pos.raw_entry) * pos.qty
            trades.append(PipelineTrade(
                pos.symbol, pos.strategy, pos.regime, pos.side.value, pos.entry_time, t,
                pos.entry_fill, exit_fill, pos.qty, pnl, pnl / pos.equity_before * 100, reason,
                pos.entry_cost + commission + fee + spread,
            ))
            del open_positions[pos.symbol]

        def open_from(order: _Order, fill: float, raw: float, t, at_open: bool) -> None:
            buy = order.side == SignalSide.LONG
            commission = costs.commission(order.qty, fill)
            fee = 0.0 if buy else costs.sell_fee(order.qty, fill)
            equity_before = marked()
            self._realized -= commission + fee
            open_positions[order.symbol] = _Open(order.symbol, order.strategy, order.regime, order.side, t,
                                                 raw, fill, order.stop, order.target, order.qty,
                                                 commission + fee, equity_before, at_open)

        for t, group in groupby(events, key=lambda e: e[0]):
            group = list(group)
            if not active and start is not None and t >= start:
                active = True
                self._realized = cfg.initial_equity
            if t.date() != day_key:
                day_key = t.date()
                day_start_equity = marked()
                entries_today = 0
                self.risk.trading_locked = False  # daily lock is per trading date (sticky within it)
                self.risk.lock_reason = ""

            for _, symbol, i in group:
                bar = self.data[symbol][i]
                # 1) working entry order
                order = orders.get(symbol)
                if order is not None:
                    if t.date() > order.expiry:
                        del orders[symbol]
                        stats["expired"] += 1
                    else:
                        long = order.side == SignalSide.LONG
                        fill = raw = None
                        at_open = False
                        if cfg.entry_mode == "next_open":
                            raw = float(bar.open)
                            fill, at_open = costs.marketable_fill(raw, buy=long), True
                        elif long and bar.open <= order.limit:
                            raw = float(bar.open)
                            fill, at_open = min(order.limit, costs.marketable_fill(raw, buy=True)), True
                        elif long and bar.low < order.limit:          # strict trade-through
                            raw = fill = order.limit
                        elif not long and bar.open >= order.limit:
                            raw = float(bar.open)
                            fill, at_open = max(order.limit, costs.marketable_fill(raw, buy=False)), True
                        elif not long and bar.high > order.limit:
                            raw = fill = order.limit
                        if fill is not None:
                            del orders[symbol]
                            open_from(order, fill, raw, t, at_open)
                        elif cfg.entry_mode == "next_open":
                            del orders[symbol]

                # 2) exits; after an intrabar fill the target is not credited on the fill bar
                pos = open_positions.get(symbol)
                if pos is not None:
                    fill_bar = pos.entry_time == t
                    target_ok = not fill_bar or pos.filled_at_open
                    gap_ok = not fill_bar or pos.filled_at_open
                    if pos.side == SignalSide.LONG:
                        if gap_ok and bar.open <= pos.stop:
                            close_position(pos, float(bar.open), "stop_gap", True, t)
                        elif bar.low <= pos.stop:
                            close_position(pos, pos.stop, "stop", True, t)
                        elif target_ok and bar.high >= pos.target:
                            close_position(pos, pos.target, "target", False, t)
                    else:
                        if gap_ok and bar.open >= pos.stop:
                            close_position(pos, float(bar.open), "stop_gap", True, t)
                        elif bar.high >= pos.stop:
                            close_position(pos, pos.stop, "stop", True, t)
                        elif target_ok and bar.low <= pos.target:
                            close_position(pos, pos.target, "target", False, t)
                last_close[symbol] = float(bar.close)

            if active and day_start_equity > 0 and (day_start_equity - marked()) / day_start_equity >= cfg.daily_loss_pct:
                self.risk.lock_trading("daily loss limit (replay)")

            # 3) decisions on the completed bars, strongest selection first
            pending_decisions = []
            for _, symbol, i in group:
                bars = self.data[symbol]
                times = self.times[symbol]
                if (symbol in open_positions or symbol in orders or i + 1 >= len(bars)
                        or (end is not None and times[i + 1] >= end) or (start is not None and times[i + 1] < start)):
                    continue
                if self.universe is not None and symbol not in self.universe.members_at(t):
                    stats["outside"] += 1
                    continue
                history = bars[max(0, i + 1 - CONTEXT_BARS):i + 1]
                selection = self.pipeline.select(history, symbol=symbol, asset_class="STK", timeframe="")
                score = selection.selected.adjusted_score if selection.selected is not None else -1.0
                pending_decisions.append((score, symbol, i, history, selection))
            pending_decisions.sort(key=lambda x: (-x[0], x[1]))
            for _, symbol, i, history, selection in pending_decisions:
                placed = self._decide(symbol, history, selection, i, t, state, orders, stats, entries_today)
                if placed:
                    entries_today += 1

            if active:
                total += 1
                in_market += bool(open_positions)
                curve.append((t, marked()))

        final_time = events[-1][0] if events else None
        for pos in list(open_positions.values()):
            close_position(pos, last_close.get(pos.symbol, pos.entry_fill), "end_of_data", True, final_time)
        stats["censored"] += len(orders)  # still working when the data ends: censored, not expired
        if curve:
            curve.append((final_time, self._realized))
        return self._result(self._realized, trades, curve, stats, in_market, total)

    def _decide(self, symbol, history, selection, i, t, state, orders, stats, entries_today) -> bool:
        cfg = self.config
        stats["decisions"] += 1
        chosen = selection.selected
        if chosen is not None and cfg.regime_filter is not None and selection.regime.regime.value not in cfg.regime_filter:
            stats["no_trade"] += 1
            return False
        if chosen is not None and not self._filters_allow(chosen.signal.side, history, t):
            stats["no_trade"] += 1
            return False
        if chosen is not None and cfg.bracket_atr is not None:
            stop, target = self._atr_bracket(chosen.signal.side, history, *cfg.bracket_atr)
            chosen = replace(chosen, signal=replace(chosen.signal, stop=stop, target=target))
            selection = replace(selection, selected=chosen)
        current = state(t)
        position_returns = {sym: _returns(self._bars_through(sym, t, 61)) for sym in exposure_symbols(current)}
        decision = self.pipeline.decide(history, symbol=symbol, asset_class="STK", timeframe="",
                                        portfolio_state=current, position_returns=position_returns,
                                        selection=selection)
        if decision.action == "NO_TRADE":
            stats["no_trade"] += 1
            return False
        if not decision.approved:
            stats["rejected"] += 1
            return False
        times = self.times[symbol]
        if self.intraday[symbol] and times[i + 1].date() != t.date():
            stats["session_closed"] += 1  # decided on the session's last bar: executor refuses
            return False
        if self.risk.trading_locked or entries_today >= cfg.max_entries_per_day:
            stats["blocked"] += 1  # executor would refuse to transmit
            return False
        signal = decision.selection.selected.signal
        orders[symbol] = _Order(
            symbol, decision.selection.selected.strategy, selection.regime.regime.value, signal.side,
            _tick(decision.proposal.entry_price), _tick(float(signal.stop)), _tick(float(signal.target)),
            float(decision.proposal.quantity), times[i + 1].date(),
        )
        stats["submitted"] += 1
        return True

    def _result(self, equity, trades, curve, stats, in_market, total) -> PipelineResult:
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
            decisions=stats["decisions"],
            no_trade_decisions=stats["no_trade"],
            rejected_by_portfolio=stats["rejected"],
            trade_log=tuple(trades),
            equity_curve=tuple(curve),
            monthly_returns_pct=tuple(r * 100 for r in monthly),
            submitted=stats["submitted"],
            unfilled_expired=stats["expired"],
            blocked_by_cap_or_lock=stats["blocked"],
            outside_universe=stats["outside"],
            blocked_session_closed=stats["session_closed"],
            censored_at_end=stats["censored"],
        )


def time_split(timeline: list[datetime], train: float = 0.6, validation: float = 0.2) -> tuple[datetime, datetime]:
    """Boundaries (validation_start, holdout_start) on a shared timeline."""
    if len(timeline) < 10:
        raise ValueError("timeline too short")
    return timeline[int(len(timeline) * train)], timeline[int(len(timeline) * (train + validation))]


__all__ = ["PipelineBacktest", "PipelineConfig", "PipelineResult", "PipelineTrade", "time_split", "field"]
