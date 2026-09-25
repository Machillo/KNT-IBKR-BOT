---
name: ibkr-execution-safety
description: Mandatory checklist for any KNT change or action involving IBKR orders, the paper/live boundary, the connection, risk limits, daily loss, kill switch, order gates, execution runners, or private account data. Use before implementing, before running any runner, and when reviewing.
---

# IBKR execution safety

## Before running anything
- Default to offline. Read-only IBKR calls: `managedAccounts`, `accountSummary`, `positions`,
  `portfolio`, `openTrades`, `reqContractDetails`, `qualifyContracts`, `reqMktData` (+cancel),
  `reqScannerData`, `reqHistoricalData`. Anything else: prove it is read-only or don't run it.
- Never run `paper_alpha.py`, `main.py` with `RUN_BROKER_SMOKE_TESTS=true`, or any `run_*paper*`/
  `*smoke*` runner unless the human asked for that exact run in this session.
- If you must avoid orders during work, set `AUTONOMOUS_TRADING_ENABLED=false` in local `.env`
  (never commit `.env`).

## Invariants (code must keep all of them)
1. `ib.placeOrder` is called only from `OrderManager._transmit` and the armed `KillSwitch`, both
   behind `PaperOrderGuard.assert_can_transmit`.
2. Paper verification (`core/paper_guard.verify_paper_account`) requires: live disabled in config,
   configured port ∈ paper ports, real socket port (`ib.client.port`) == configured, connected,
   every managed account matches `^D[UF]\d+$`, unambiguous target account. Re-run per order.
   The API has no explicit paper flag — never replace this with a port-only test.
3. `PaperExecutionEngine.submit` order of gates: enabled → guard → connected → session policy
   (missing = closed) → risk manager present/unlocked → request sanity → shorts disabled → daily
   cap → STK + integer qty → geometry → fresh LIVE reference within deviation → `evaluate_trade`
   → duplicate (symbol/conId/in-flight) → bracket (all legs guard-checked first).
4. Autonomous loop armed only by `AUTONOMOUS_TRADING_ENABLED=true` + literal `AUTONOMOUS_PAPER_ACK`.
5. Supervisor locks entries if the session is not verified paper, on monitoring errors, on
   non-flat startup (`REQUIRE_FLAT_STARTUP`), and on daily loss (sticky across restarts).
6. Armed kill switch on an unverified session does nothing (it could otherwise cancel/liquidate
   a human's live account). Dry-run is the default until armed flow is paper-tested.
7. Live decisions use completed bars only (`complete_only=True`).
8. Logs mask accounts (`mask_account`); `state/`, `reports/`, logs, `*.db`, `.env` are git-ignored.

## When changing risk/execution code
- Never loosen a limit or gate; tightening needs a reason in the commit.
- Add a test that fails if the guard/gate is removed (fake IB with `managedAccounts`,
  `client.port`, `isConnected`, `placeOrder` recorder; assert `placed == []` on refusal).
- New order type/asset class: tick normalization, quantity rules, multiplier, session, cost and
  risk model first; otherwise the executor must reject it (it does: STK only).
- Get `execution-safety-auditor` review with the diff before handing off.

## Known gaps (keep honest)
Holiday/half-day calendar absent; shortability/borrow/SSR not modeled (shorts disabled);
no time-based exit; no multi-day drawdown lock; kill-switch armed flow never paper-tested.
