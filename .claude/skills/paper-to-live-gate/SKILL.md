---
name: paper-to-live-gate
description: Criteria for moving KNT along BACKTEST → OOS/WALK-FORWARD → SHADOW → IBKR PAPER → SMALL LIVE → SCALE, and for stating readiness honestly. Use when asked "are we ready", before any paper run plan, and when writing a readiness/handoff report.
---

# Promotion gates

A stage is claimed only when EVERY item is evidenced (code path, test, report). Otherwise report
the stage as NOT READY and list the missing items. LIVE is never declared by Claude.

## BACKTEST READY (the simulator can be trusted to rank ideas)
- Next-bar fills, gap-aware stops, IBKR commission model, spread/slippage separated, integer qty.
- Daily-equity Sharpe/Sortino; mark-to-market DD; regression tests for each of these.

## VALIDATED CANDIDATE (a strategy/selector configuration may go to shadow)
- Frozen config evaluated once on an untouched HOLDOUT; positive net expectancy under stressed
  costs; ≥ 30 holdout trades; no single symbol/period dominating; DD within the risk budget.
- Result recorded in `docs/experiments/` with the number of variants tried.

## SHADOW READY
- Selector uses only admissible evidence; completed bars; decisions + would-be orders journaled;
  a way to score shadow decisions later (forward returns) exists.
- Runs read-only; `AUTONOMOUS_TRADING_ENABLED=false`.

## PAPER READY (first human-supervised paper run)
- Verified-paper guard (account prefix + socket port + config), re-verified per order, tested.
- Executor re-applies hard risk; fresh LIVE quote (market data type 1) required; daily entry cap;
  shorts disabled unless shortability modeled; integer qty; duplicates blocked.
- Kill switch dry-run default; daily loss lock sticky; flat-startup lock.
- A VALIDATED CANDIDATE exists, or the run is explicitly a plumbing test with minimal size
  (e.g. `run_knt_signal_paper_once`, max 1 share) — say which.
- Human run plan: who watches, duration, how to stop, what is checked afterwards.

## SMALL LIVE (human decision only)
Requires: sustained paper record matching backtest expectations (slippage, fill rate, costs),
armed kill switch tested on paper, operational runbook, explicit written human authorization,
and a separate reviewed change to allow live. Claude never flips live flags.

## SCALE
Only after live results match paper within tolerance over a meaningful sample.
