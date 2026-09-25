# Overnight progress — checkpoint (session 2)

Session 1 (`claude/overnight-hardening`, up to 868f7d9) was merged by Kenneth into
`feature/paper-alpha` via PR #2 (merge commit 0356044). Session 2 work lives on a NEW branch,
`claude/overnight-session2`, replayed onto the current `feature/paper-alpha` (identical tree to the
old branch tip), with its own draft PR (base `feature/paper-alpha`). Nothing merged into `main`.
No orders, no IBKR connection this session; local `.env` keeps `AUTONOMOUS_TRADING_ENABLED=false`.

## Session 1 (see git log up to 868f7d9)
Paper guard, executor gates, backtester v2, leak-free validation, pipeline replay, protocol v1,
H1–H9 all REJECT, null model, shadow journal v1. Tests 124 → 275.

## Session 2 — blocks
| block | key commits |
|---|---|
| Point-in-time discovery funnel (every scanner row, status, reason, sources) + cycle metadata + decision context | 1bf7796 |
| Idempotent shadow scorer (TRADE + NO_TRADE counterfactual, MAE/MFE, fwd 1/5/20, dedup, alignment, LIMIT/DAY) | 76076d2, d2a8d6e, fe8a7ad |
| Shared `DecisionPipeline` (runtime = replay); replay v3 with real allocator/admission, LIMIT/DAY entries, caps, score-ordered cycles | a4204e5, 955edc2 |
| Point-in-time universe providers (journal, CSV with delistings) + replay wiring + journal bar fetcher | a4204e5, 9198f35, 3fc0004 |
| Risk: sticky multi-day drawdown lock; sector concentration with fail-closed metadata; working orders count for correlation; in-cycle exposure update | 8ae6711, e8e415f, 955edc2, (latest) |
| Kill-switch paper drill runner + runbook (NOT run) | f5eb166 |
| Order-incapable shadow-only runner for forward evidence | 083ecbe |
| Cohort-neutral event study + round 3 families F1–F6 (all REJECT) + stop condition + forward hypotheses | 9f3c4a3 … 5b6d511, fd395ff |
| `market-discovery` skill; docs POINT_IN_TIME_DATA, REPLAY_PARITY, KILL_SWITCH_DRILL | 59ddd2b, 9198f35 |

| Safety review round 3: drawdown failures never block the kill switch, repo-anchored state, readonly sessions refused by the guard, drill limits, ACKs refused from .env, shadow-only isolated state, test isolation of state/ | 961b3d8, eb92db5, 61270a4 |
| Diagnostic: baseline + null model under replay v3 | 45b3476 |

Reviews this session: execution-safety ×3 (0 blockers; all should-fix fixed), quant-methodology ×2
(3 + 2 blockers, all fixed), trading-architect (13 documented divergences; 2 runtime fixes).
Tests: 275 → 351 passing; conftest fails the run if the real state/ directory changes.

## Research status
- Tests on protocol-v1 VALIDATION: **15** (H1–H9, F1–F6) → stop searching this dataset.
- HOLDOUT (≥ 2025-03-01): unused.
- No KEEP. Most informative: F6 — selector-logic LONG picks on daily/4h bars underperform the
  cohort after costs in VALIDATION (|t| ≈ 3.4–3.6); F1 momentum is a survivorship artefact.
- Replay v3 (executor-faithful entries) makes absolute results positive, but random entries under
  the same mechanics match it: execution mechanics in a rising cohort, not selection.
- Forward-only hypotheses FWD1–FWD3 pre-registered for shadow data recorded from now on.

## Next actions
1. Human: review PR #2; start `run_shadow_only.py` during market hours (read-only; no orders)
   to begin forward evidence; weekly `run_score_shadow.py` and `run_fetch_journal_bars.py`.
2. Human: first supervised paper plumbing test (`docs/PAPER_RUN_PLAN.md`, MARKET_DATA_TYPE=1)
   and kill-switch drill (`docs/KILL_SWITCH_DRILL.md`).
3. Decide on an external survivorship-free dataset (`docs/POINT_IN_TIME_DATA.md`).
