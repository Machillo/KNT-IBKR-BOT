# Runtime vs replay parity

Shared (same code): `engine/decision.DecisionPipeline` → `StrategySelector` →
`PortfolioAllocator` → `PortfolioAdmissionCoordinator` (RiskManager, PortfolioBrain,
CrossExposureGuard). Asserted in `tests/test_replay_parity.py`.

Fixed this session: replay correlation alignment, working orders counted as exposure (runtime
and replay), in-cycle state update after a runtime submission, completed bars for runtime
position history, LIMIT/DAY entries, strict trade-through, session-close refusal, daily cap.

## Remaining divergences (from the architecture review; bias = effect on replay results)

| # | divergence | bias | status |
|---|---|---|---|
| 1 | Runtime decides on **1-hour** bars (~147 bars of context); development profiles are 4h/daily with up to 450 bars | not comparable | the only 1h data (`intraday_1y`) is HOLDOUT; forward shadow data fixes this |
| 2 | Context length changes the regime detector (EMA50 vs EMA200 switch at 205 bars) | unknown | documented |
| 3 | Runtime waits until bar start + bar size to call a bar complete (first RTH bar is 30 min) | slightly optimistic | documented |
| 4 | Runtime selector has a learning store (bonuses/AVOID once admissible evidence exists); replay has none | unknown, grows over time | documented |
| 5 | Sector metadata required at runtime; replay has none (sector cap never binds) | optimistic | tested |
| 6 | Runtime pending exposure includes filled brackets' stop/target child orders (overstates gross) | replay optimistic (runtime more conservative) | documented |
| 7 | Executor's fresh-quote (≤1.5 %) check has no replay equivalent | unknown | documented |
| 8 | Runtime session buffers (first 5 / last 15 min, holidays); replay only refuses last-bar decisions (none on daily) | optimistic | partial tests |
| 9 | Runtime daily cap counts journal INTENT rows per UTC date (incl. failures); replay counts submissions | slightly optimistic | tested separately |
| 10 | Runtime has the sticky multi-day drawdown lock and transmission-error locks; replay does not | optimistic | documented |
| 11 | Runtime limits come from `.env`; replay uses `PipelineConfig` defaults | depends on `.env` | documented |
| 12 | Runtime candidates = top-12 by liquidity score in scanner order; replay decides every universe member, strongest selector score first | optimistic if score predicts returns | documented |
| 13 | Runtime entry parent uses IBKR Adaptive algo; replay fills exactly at the limit | minor | documented |

## Multi-asset blockers found
Replay hard-codes `"STK"` and a 0.01 tick; shadow assumes USD/SMART and a 1-hour timeframe;
the allocator has no contract multiplier; costs are US-stock only; `PortfolioSnapshot` has no
currency; the executor rejects non-STK (intended). Each must be generalized before another
asset class can feed decisions (see `market-discovery` skill).
