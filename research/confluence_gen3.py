from __future__ import annotations

from dataclasses import dataclass

from backtest.engine import BacktestEngine
from backtest.monthly_target import summarize_months
from market.history import PriceBar
from research.confluence_lab import candidate_strategies
from research.splits import Window, run_window


@dataclass(frozen=True)
class Gen3Result:
    symbol: str
    profile: str
    strategy: str
    train_monthly_pct: float
    validation_monthly_pct: float
    test_monthly_pct: float
    train_dd_pct: float
    validation_dd_pct: float
    test_dd_pct: float
    test_positive_month_rate_pct: float
    test_trades: int
    # Validation evidence used by later selection stages (Gen4). TEST columns are
    # reported once and must never be used to admit or rank candidates.
    validation_positive_month_rate_pct: float = 0.0
    validation_trades: int = 0


def split_train_validation_test(bars: list[PriceBar]) -> tuple[list[PriceBar], list[PriceBar], list[PriceBar]]:
    if len(bars) < 300:
        return [], [], []
    train_end = int(len(bars) * .60)
    validation_end = int(len(bars) * .80)
    return bars[:train_end], bars[train_end:validation_end], bars[validation_end:]


def gen3_windows(n: int) -> tuple[Window, Window, Window]:
    train_end = int(n * .60)
    validation_end = int(n * .80)
    return (Window("TRAIN", 0, train_end), Window("VALIDATION", train_end, validation_end),
            Window("TEST", validation_end, n))


def _run(bars: list[PriceBar], strategy: object, window: Window):
    engine = BacktestEngine(initial_equity=10_000.0, risk_pct=.01, max_position_pct=.10)
    return run_window(engine, bars, strategy, window)


def _selection_score(result) -> float:
    monthly = summarize_months(result)
    pf = result.profit_factor
    finite_pf = 0.0 if pf is None else min(3.0, 3.0 if pf == float("inf") else float(pf))
    return (monthly.compounded_monthly_pct * 4.0 + monthly.positive_month_rate_pct * .08
            + finite_pf * 2.0 - result.max_drawdown_pct * .75)


def select_and_test(symbol: str, profile: str, bars: list[PriceBar]) -> Gen3Result | None:
    """Select on TRAIN+VALIDATION only; evaluate the single winner once on TEST.

    Windows read earlier bars for warmup but count only entries inside them.
    """
    if len(bars) < 300:
        return None
    train, validation, test = gen3_windows(len(bars))
    survivors = []
    for strategy in candidate_strategies():
        trr = _run(bars, strategy, train)
        tr = summarize_months(trr)
        if trr.trades < 8 or tr.compounded_monthly_pct <= 0 or trr.max_drawdown_pct > 20:
            continue
        var = _run(bars, strategy, validation)
        va = summarize_months(var)
        if var.trades < 3 or va.compounded_monthly_pct <= 0 or var.max_drawdown_pct > 20:
            continue
        survivors.append((.35 * _selection_score(trr) + .65 * _selection_score(var), strategy, trr, var))
    if not survivors:
        return None
    survivors.sort(key=lambda x: x[0], reverse=True)
    _, winner, trr, var = survivors[0]
    ter = _run(bars, winner, test)
    tr, va, te = summarize_months(trr), summarize_months(var), summarize_months(ter)
    return Gen3Result(
        symbol, profile, str(winner.name), tr.compounded_monthly_pct, va.compounded_monthly_pct,
        te.compounded_monthly_pct, trr.max_drawdown_pct, var.max_drawdown_pct, ter.max_drawdown_pct,
        te.positive_month_rate_pct, ter.trades, va.positive_month_rate_pct, var.trades,
    )
