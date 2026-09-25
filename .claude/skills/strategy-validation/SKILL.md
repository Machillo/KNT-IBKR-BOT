---
name: strategy-validation
description: Protocol for KNT backtesting, walk-forward/OOS, holdout, cost modelling, metrics, strategy research loops, learning/promotions and reporting performance honestly. Use whenever you run or change a backtest, validation, research sweep, selector learning, or state any return/Sharpe/edge number.
---

# Strategy validation

## Simulator contract (backtest/engine.py)
- Signal from bars[:i+1] (completed); entry at bar i+1 open; stop/target from the signal.
- Gap through stop → exit at the open (worse than the stop). Gap through target → exit at the
  target price (conservative; a real limit would fill at the better open). Same bar stop+target
  → stop.
- Costs via `backtest/costs.py::CostModel`: IBKR per-share commission with min/max per order,
  half-spread and slippage in bps, all per side and reported separately. Scenarios: baseline,
  stressed (≥2× spread+slippage). Never compare strategies under different cost models.
- Integer shares for STK (`quantity_step=1`); if the risk budget buys < 1 share → no trade.
- Metrics (`backtest/metrics.py`): Sharpe/Sortino from DAILY mark-to-market equity returns,
  annualized × √252, rf = 0; CAGR, monthly returns, expectancy, PF, max DD, exposure.
  The old per-trade "sharpe" is kept only as `trade_sharpe` and is not a Sharpe ratio.

## Time splits (research/splits.py)
- Chronological only. Standard cycle: TRAIN 60 % → VALIDATION 20 % → HOLDOUT 20 %.
- Warmup: a window's backtest may read earlier bars for indicators, but only trades whose ENTRY
  bar is inside the window count (`BacktestEngine.run(..., trade_start=k)`).
- Walk-forward evidence stores OOS only for decisions; TRAIN rows are diagnostic.
- HOLDOUT is used exactly once per frozen candidate set. Record the decision in
  `docs/experiments/` BEFORE looking. After use it becomes history, not a test.
- A new development cycle on a used strategy needs a new split (e.g. later data) — never re-tune
  on the old holdout.

## Research loop (per hypothesis)
1. Hypothesis + baseline + the single change.
2. Run on TRAIN; compare on VALIDATION against the baseline under identical costs.
3. KEEP only if VALIDATION improves on a pre-declared metric AND trade count is adequate
   (≥ 30 trades aggregate, ≥ 5 symbols contributing) AND it survives the stressed cost scenario.
4. Log: hypothesis, baseline, change, result, KEEP/REJECT/INCONCLUSIVE, reason
   (`docs/experiments/LOG.md`). REJECT is a valid, useful result.
5. No blind grids. Count every variant tried; report N alongside the winner.

## Admissible evidence for operational learning
Selector bonuses and promotions may only use OOS/VALIDATION evidence produced by the corrected
engine (`evidence_version >= 2`) with enough trades. Full-sample backtest promotions are
research-only (`context_promotions` operational use disabled). Missing clean evidence → no bonus.

## Reporting template
Span · bar size · symbols (and how chosen; disclose survivorship) · split · cost scenario ·
trades · net return · CAGR/monthly · max DD · daily Sharpe · PF · expectancy · per-period and
per-symbol stability · holdout used? · verdict. Never extrapolate backtests into income promises.

## Known data limits
`reports/history_cache`: 38 hand-picked current US names (survivorship bias), IBKR TRADES bars
(split-adjusted, not dividend-adjusted), 1y/1h, 5y/4h, 10y/1d. Historical dynamic discovery
(scanner output) cannot be reproduced from it — do not simulate it.
