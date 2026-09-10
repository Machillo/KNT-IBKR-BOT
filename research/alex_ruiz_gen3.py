from __future__ import annotations

from dataclasses import dataclass

from backtest.engine import BacktestEngine
from backtest.monthly_target import summarize_months
from market.history import PriceBar
from research.alex_ruiz_lab import candidate_strategies


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


def split_train_validation_test(bars: list[PriceBar]) -> tuple[list[PriceBar], list[PriceBar], list[PriceBar]]:
    """Chronological 60/20/20 split. Test is never used for strategy selection."""
    if len(bars) < 300:
        return [], [], []
    train_end = int(len(bars) * 0.60)
    validation_end = int(len(bars) * 0.80)
    return bars[:train_end], bars[train_end:validation_end], bars[validation_end:]


def _run(bars: list[PriceBar], strategy: object):
    return BacktestEngine(initial_equity=10_000.0, risk_pct=0.01, commission_bps=2.0, slippage_bps=2.0, max_position_pct=0.10).run(bars, strategy)


def _selection_score(result) -> float:
    monthly = summarize_months(result)
    pf = result.profit_factor
    finite_pf = 0.0 if pf is None else min(3.0, 3.0 if pf == float("inf") else float(pf))
    return (
        monthly.compounded_monthly_pct * 4.0
        + monthly.positive_month_rate_pct * 0.08
        + finite_pf * 2.0
        - result.max_drawdown_pct * 0.75
    )


def select_and_test(symbol: str, profile: str, bars: list[PriceBar]) -> Gen3Result | None:
    """Select on train+validation only, then open the untouched final 20% once."""
    train, validation, test = split_train_validation_test(bars)
    if not train or not validation or not test:
        return None

    survivors: list[tuple[float, object, object, object]] = []
    for strategy in candidate_strategies():
        train_result = _run(train, strategy)
        train_monthly = summarize_months(train_result)
        if train_result.trades < 8 or train_monthly.compounded_monthly_pct <= 0 or train_result.max_drawdown_pct > 20:
            continue
        validation_result = _run(validation, strategy)
        validation_monthly = summarize_months(validation_result)
        if validation_result.trades < 3 or validation_monthly.compounded_monthly_pct <= 0 or validation_result.max_drawdown_pct > 20:
            continue
        score = 0.35 * _selection_score(train_result) + 0.65 * _selection_score(validation_result)
        survivors.append((score, strategy, train_result, validation_result))

    if not survivors:
        return None
    survivors.sort(key=lambda item: item[0], reverse=True)
    _, winner, train_result, validation_result = survivors[0]
    test_result = _run(test, winner)
    tr = summarize_months(train_result)
    va = summarize_months(validation_result)
    te = summarize_months(test_result)
    return Gen3Result(
        symbol=symbol,
        profile=profile,
        strategy=str(winner.name),
        train_monthly_pct=tr.compounded_monthly_pct,
        validation_monthly_pct=va.compounded_monthly_pct,
        test_monthly_pct=te.compounded_monthly_pct,
        train_dd_pct=train_result.max_drawdown_pct,
        validation_dd_pct=validation_result.max_drawdown_pct,
        test_dd_pct=test_result.max_drawdown_pct,
        test_positive_month_rate_pct=te.positive_month_rate_pct,
        test_trades=test_result.trades,
    )
