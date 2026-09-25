# Overnight progress — checkpoint (end of session 1)

Branch `claude/overnight-hardening` (pushed; draft PR, base `feature/paper-alpha`), created from
`feature/paper-alpha` @ 0b9e191 (the real project; `main` is only an initial commit; PR #1
unmerged). Never merged. No IBKR connection and no orders during the session; local `.env` set
`AUTONOMOUS_TRADING_ENABLED=false`.

## Baseline → now
Tests 124 → 275 passing. Mutation checks: removing the paper-guard call, the gap-stop fix or the
OOS-only evidence filter each makes tests fail (3/2/1 failures).

## Blocks done
| Block | Commits |
|---|---|
| .gitignore (state/, reports/, logs, *.db, .env*) + hygiene tests | a378276 |
| Paper guard (DU/DF on every managed account + real socket port + config, per order), executor gates (broker equity risk re-check, live two-sided quote, no shorts, whole shares, daily cap, conId duplicates, INTENT journal, unwind→lock), ACK for autonomous loop, completed bars, masked/redacted account ids | 0fa8b3e, 0ee0d9b |
| Broker-calendar session policy (holidays/half days, 5/15-min buffers, fail closed) | bfd47a4 |
| Static order-path guards; README/.env.example | 616c67f, fc9f521 |
| CLAUDE.md, 4 agents, 5 skills | 0b3072e, 4a199fe |
| Backtester v2: gap stops, IBKR fixed commissions, spread/slippage, sell fee, borrow, integer qty, daily Sharpe, full-cost MTM, trade_start | d356f0a, 15288fb |
| Validation: warm windows, OOS-only engine-v2 evidence, regime at signal time, monthly-target holdout, Gen2/Gen3/Gen4 leakage fixes | 04c76b8, 15288fb |
| Full-sample context promotions removed from operational selector | 96329f4 |
| Pipeline (selector) backtest, calendar protocol v1, experiments H1–H9, null model | 2521a8f, d4ef686, 4ce5014, 94e45d8 |
| Shadow journal: point-in-time discovery + every decision + scorer | 6e5fa61 |
| Release-gate fixes: journal non-fatal, paper run plan | (latest) |

Independent reviews: execution-safety (0 blockers; should-fix fixed), quant-methodology
(1 blocker — Gen2 ranked on holdout — fixed; should-fix fixed), release-gate (git/tests PASS).

## Quant result (protocol v1, details in docs/experiments/LOG.md)
9 pre-registered variants + baseline on VALIDATION; all REJECT. Live selector ≈ zero expectancy
after costs (PF 0.89–1.06). Null model: selector beats random entries on 4h (~1.8 sd, weak) but
is worse than random longs on daily. HOLDOUT (≥ 2025-03-01) unused. No edge. Data (38-name
hindsight cohort, no delisted names) is the main blocker.

## Readiness
BACKTEST: engine ready, data not. VALIDATED CANDIDATE: none. SHADOW: partial (journal yes,
scoring runner no). PAPER: ready only for a supervised 1-share plumbing test
(docs/PAPER_RUN_PLAN.md). LIVE: NOT READY.

## Next actions
1. Human: review draft PR; run docs/PAPER_RUN_PLAN.md (needs MARKET_DATA_TYPE=1 + live data).
2. Code: runner that scores journaled shadow decisions against later bars (read-only history).
3. Research: point-in-time universe (accumulate shadow discovery snapshots; or external
   survivorship-free data) before any new strategy round; start protocol v2 on new data.
4. Open issues: sector exposure unmodelled; kill-switch armed flow never paper-tested; no
   multi-day drawdown lock; shortability not modelled (shorts disabled).
