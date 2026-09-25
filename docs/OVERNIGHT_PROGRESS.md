# Overnight progress — checkpoint

Branch `claude/overnight-hardening` (pushed), based on `feature/paper-alpha` @ 0b9e191 (the real
project; `main` is only an initial commit; PR #1 unmerged). Never merged to `main`. No IBKR
connection and no orders during this session; local `.env` set `AUTONOMOUS_TRADING_ENABLED=false`.

## Baseline (start)
124 tests passing. Audit: port-only paper detection; autonomous flag alone sent paper brackets;
public repo with unignored `state/*.db`; stop-gap optimism, bps-only costs, per-trade "Sharpe";
walk-forward mixed TRAIN/OOS with OOS windows < warmup; monthly-target OOS inside full sample;
Gen4 selected on test; full-sample context promotions steering the live selector; no edge
(Gen3 holdout mean −0.03 %/mo).

## Blocks
| # | Block | Status | Commits |
|---|---|---|---|
| 0 | .gitignore + hygiene tests | done | a378276 |
| 2 | Paper guard, executor gates, ACK, complete bars, masked logs | done + reviewed | 0fa8b3e, 0ee0d9b |
| 1 | CLAUDE.md, 4 agents, 5 skills | done | 0b3072e |
| 3 | Backtester: gap stops, IBKR costs, integer qty, daily Sharpe, trade_start | done | d356f0a |
| 4 | Validation: splits, warm OOS, OOS-only evidence, holdouts, Gen4 leakage | done | 04c76b8 |
| 5 | Context promotions off in operational selector | done | 96329f4 |
| — | Quant review fixes (Gen2 holdout ranking BLOCKER, borrow, MTM, overlap) | done | 15288fb |
| 6 | Pipeline (selector) backtest + runner | done | 2521a8f |
| 7 | Research protocol v1 (calendar split) + pre-registered H0–H6 | done: all REJECT | d4ef686, c87cb8c |
| — | Broker-calendar session policy (holidays/half days, fail closed) | done | bfd47a4 |
| — | Static order-path guard tests; README/.env.example | done | 616c67f, fc9f521 |
| 8 | Round 2 H7–H9 (symbol/market trend filter, ATR brackets) | running | 4ce5014 |

Independent reviews: execution-safety (no blockers; SHOULD-FIX all fixed), quant-methodology
(1 blocker + should-fix all fixed). Tests: 258 → see latest commit.

## Key decisions
- Paper = every managed account DU/DF + real socket port == configured paper port + live disabled;
  re-verified per order. API has no explicit paper flag.
- Executor refuses: delayed/frozen quotes, shorts, fractional qty, missing broker equity.
- Evidence for live learning: OOS rows, engine v2+, latest completed run only.
- Calendar protocol v1: TRAIN < 2023-09-01 ≤ VALIDATION < 2025-03-01 ≤ HOLDOUT (all profiles).
  `intraday_1y` is entirely holdout. `live_default` baseline was seen once on fraction splits
  before the protocol (disclosed in research/protocol.py).

## Baseline pipeline (fraction splits, pre-protocol, for the record)
live_default: 1h TRAIN −2.3 % / VAL −9.6 %; 4h TRAIN −11.2 % / VAL −2.5 %;
1d TRAIN +1.3 % (PF 1.00) / VAL +24.9 % (PF 1.29). No consistent edge.

## Next action
Read results of `run_experiments.py --yearly` (H0–H6, TRAIN+VALIDATION, 4h & 1d), apply the
pre-registered decision rule in docs/experiments/LOG.md, then continue the hypothesis loop.
