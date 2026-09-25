---
name: knt-trading-core
description: Core architecture, data flow, conventions and "where does X live" map for KNT-IBKR-BOT (IBKR/ib_async trading research system). Use for any KNT task before changing code — discovery, regime, strategy selector, allocation, admission, execution, research/learning, runners, persistence.
---

# KNT trading core

## Runtime flow (paper_alpha.py)
```
PaperSupervisor (poll): AccountService → paper verification (core/paper_guard) → DailyLossGuard
                        → KillSwitch (dry-run default; armed only on verified paper) → state/risk_state.json
shadow_loop (interval): PortfolioStateService → MarketIntelligenceService
   → IBKRDiscoveryService.scan_many(US_STOCK_OPPORTUNITY_UNIVERSE: 4 scanners)
   → common-stock filter → quote snapshots (budget) → LiquidityRanker (spread/price)
   → top-N → ContinuousResearchScheduler (walk-forward research, bounded)
   → HistoricalDataService.bars(complete_only=True)
   → StrategySelector: RegimeDetector + SINGLE_ASSET_STRATEGIES + regime bonus + learning bonus
   → argmax ≥ threshold or NO_TRADE
   → PortfolioAllocator → PortfolioAdmissionCoordinator(RiskManager, PortfolioBrain, CrossExposureGuard)
   → PaperExecutionEngine.submit (guard, risk re-check, fresh live quote, caps) → bracket
```
`main.py` = older core (supervisor + optional smoke). Pairs are evaluated and logged only.

## Where things live
| Concern | File |
|---|---|
| Env config + validation | `config/config.py` |
| Paper verification / order authorization | `core/paper_guard.py` |
| All order transmission | `core/order_manager.py` (`_transmit`) |
| Hard risk | `risk/risk_manager.py`; daily lock `risk/daily_guard.py`; sticky state `risk/state_store.py` |
| Kill switch | `risk/kill_switch.py` |
| Sizing / admission | `portfolio/allocation.py`, `portfolio/admission.py`, `brain.py`, `exposure.py` |
| Strategies | `strategies/library.py` (+ `confluence.py`, baseline `momentum.py`) |
| Regime | `market/regime.py`; session `market/session.py` |
| Simulator / costs / metrics | `backtest/engine.py`, `backtest/costs.py`, `backtest/metrics.py` |
| Validation / splits | `backtest/validation.py`, `research/walkforward.py`, `research/splits.py` |
| Learning / promotions | `research/learning.py`, `research/context_promotions.py`, `research/performance.py` |
| Pipeline (selector) backtest | `research/pipeline_backtest.py` |
| Persistence | `state/strategy_performance.db` (SQLite, local only), `state/risk_state.json` |

## Conventions
- Strategies are pure: `evaluate(bars) -> StrategySignal(side, score, entry, stop, target, reason)`,
  must only read `bars` (all completed). They never touch IBKR or orders.
- Scores 0–100; selector threshold default 55. `SignalSide.FLAT` = no setup.
- New strategy = class with `name`, `warmup`, `evaluate`; register deliberately; it must be
  validated (strategy-validation) before it can influence the live selector.
- Discovery is scanner-plan driven (`ScannerPlan`, `UniversePlan`); no permanent ticker whitelist
  in runtime code. `backtest/universes.py` cohorts are diagnostic-only.
- Asset class today: STK/USD/SMART, tick 0.01, integer shares, US RTH session. Multi-asset needs its
  own contract details (multiplier, tick, min size), session policy, cost model and risk model first.
- SQLite stores are append-only research memory; never delete history — version or supersede it.

## Runners
IBKR-connected (read-only): `run_universe_probe`, `run_backtest`, `run_strategy_suite`, `run_research`,
`run_bulk_research`, `run_validation_suite`, `run_monthly_target_suite`, `run_growth_projection`.
Order-capable (paper, ACK-gated): `paper_alpha` (autonomous ACK), `main` (smoke flag),
`run_broker_smoke_once`, `run_paper_bracket_smoke`, `run_knt_signal_paper_once`.
Offline (cache only): `run_backtest_analysis`, `run_import_context_promotions`, `run_confluence_*`,
`run_pipeline_backtest`, `run_holdout_study`.
