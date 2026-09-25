# Runtime vs replay parity

Shared code, used by runtime, order-free shadow and replay:
- `engine/decision.DecisionPipeline`: `StrategySelector` → `PortfolioAllocator` →
  `PortfolioAdmissionCoordinator` (RiskManager, PortfolioBrain, CrossExposureGuard);
- `engine/decision.DECISION_CONTEXT_BARS`: the selector sees exactly the same number of
  completed bars;
- `execution/pretrade.evaluate`: the executor's pure pre-transmission checks, in the
  executor's order (session → lock → sanity → side → short policy → daily cap →
  instrument/quantity → tick normalization → geometry → fresh reference);
- `execution/pretrade.hard_risk_refusal`: the hard-risk re-check on tick-rounded prices.

These are asserted in `tests/test_replay_parity.py`, `tests/test_pretrade.py` and
`tests/test_shadow_research_path.py`. The replay is `PIPELINE_VERSION = 4`. Results from
v3 or earlier are not comparable with v4.

Classes:
- **A**: can be eliminated in code now.
- **B**: needs external or forward data.
- **C**: inherent to replaying history.
- **D**: methodological risk; bias documented, tested where possible.

## Divergences: before and after this round

| # | Divergence | Class | Before | After |
|---|---|---|---|---|
| 1 | The runtime decides on **1-hour** bars. Development profiles are 4h/daily. | B+D | Not comparable. The only 1h data (`intraday_1y`) is HOLDOUT. | Unchanged. Forward shadow data (journal v2 + `run_fetch_journal_bars.py`) is the fix. |
| 2 | Context length: replay used 450 bars, runtime ~147 (30 D of RTH hours). The regime detector uses EMA200 at ≥ 205 bars and EMA50 below. | A | Replay ran EMA200 regimes and the runtime ran EMA50. | **Eliminated.** Both take the last `DECISION_CONTEXT_BARS` = 140 completed bars. The runtime requests 45 D and truncates. |
| 3 | Bar availability: the runtime calls a bar complete at start + size. | A | Documented. | **Equivalent.** Replay places the order at bar *i+1* (available at the end of *i*). Keys are exchange time (UTC bars are now converted, see N7). |
| 4 | The runtime selector had a learning store (bonus/AVOID); replay has none. | B/D | Learning drifted inside the forward window. | **Eliminated for shadow-only.** Learning is frozen (`learning_enabled=False`) and `selector_bonus`/`learning_mode` are journaled. `paper_alpha` still learns (runtime choice). |
| 5 | Sector metadata is required at runtime; replay has none, so the sector cap never binds. | B | Replay optimistic. | Unchanged. Sector is journaled per decision from now on (v2). Needs point-in-time sectors (docs/POINT_IN_TIME_DATA.md). |
| 6 | Runtime pending exposure includes the stop/target children of filled brackets. | A (loosens a gate) | Runtime more conservative. | **Kept on purpose.** Removing it would relax a live gate. Bias: replay slightly optimistic. |
| 7 | The executor's fresh-quote check (≤ 1.5 %, live data type 1) has no replay equivalent. | C | Silently skipped. | **Explicit.** Replay runs pretrade with `reference_available=False`, so the check is reported as unchecked. Shadow-only now runs it on a real read-only quote. Quote history would make it B. |
| 8 | Session buffers (first 5 / last 15 min, half-days, holidays). | A | Replay only refused last-bar decisions. | **Equivalent for 1h RTH bars.** Decisions become available at XX:00. The one at the close is refused (next bar on another date), half-day closes too, and no XX:00 falls inside a buffer. The check goes through `pretrade` (`market_session_closed`). |
| 9 | The daily cap counts journal INTENT rows per UTC date, including failures; replay counts submissions. | C | Slightly optimistic. | Equivalent except transmission failures, which cannot be simulated. RTH never crosses a UTC date. |
| 10 | Sticky multi-day drawdown lock. | A | Missing in replay (optimistic). | **Eliminated.** Replay tracks the high-water mark of marked equity and locks for good at `max_drawdown_pct`. Remaining difference: replay marks per bar, the supervisor per poll (C). |
| 11 | The runtime takes its limits from `.env`; replay used `PipelineConfig` defaults. | A | Could drift. | **Eliminated on demand.** `PipelineConfig.from_bot_config` and `run_pipeline_backtest.py --config-from-env`. The defaults still equal the code defaults. |
| 12 | The runtime analyses the top-12 eligible names by liquidity; replay analysed every member. | A (journal) / B (CSV) | Optimistic. | **Eliminated for the journal universe.** `JournalUniverse(top_n=SHADOW_MAX_CANDIDATES)` keeps the same liquidity order (ties by scanner rank). CSV universes stay B. |
| 13 | The entry parent uses the IBKR Adaptive algo; replay fills exactly at the limit, with a strict trade-through. | C | Minor. | Unchanged. Run cost/slippage stress (`--cost stressed` / `severe`) and do not treat touch fills as queue-guaranteed. |

## Additional divergences found by the architecture review of this round

| # | Divergence | Class | After |
|---|---|---|---|
| N1 | Shadow-only and unarmed `paper_alpha` did not accumulate same-cycle approvals. | A | **Eliminated for shadow-only.** SHADOW_SUBMIT entries become pending exposure for the rest of the cycle *and* of the UTC day (virtual book). The book is conservative: a would-be DAY order occupies capacity all day, even if it would have filled and exited, and it is gone the next day, so overnight positions are not modelled (D). Unarmed `paper_alpha` still does not accumulate (only shadow-only is the evidence source). |
| N2 | Shadow-only skipped every executor check. | A | **Eliminated.** Pretrade + hard risk + daily cap + duplicate check on a fresh read-only quote give `SHADOW_SUBMIT` or `SHADOW_BLOCKED:<reason>`. Broker-only checks (paper guard, connection, broker equity) cannot run in a read-only session. |
| N3 | Shadow-only has its own drawdown high-water mark (state/shadow_only). | C | Documented. The research lock also mirrors the trading bot's persisted sticky kill and drawdown lock, read-only. |
| N4 | Research filters bypass the pipeline. | A | **Flagged.** `PipelineResult.runtime_equivalent` is False for any research-only knob. |
| N5 | Tick rounding came before the executor's risk re-check but not in replay. | A | **Eliminated.** Replay re-checks hard risk on the rounded prices through `pretrade.hard_risk_refusal`. |
| N6 | Re-decision cadence: the runtime decides up to `SHADOW_INTERVAL_SECONDS` after a bar completes; replay decides at completion and may fill at the next bar's open. | C/D | Documented. Bias: replay optimistic on open-of-bar fills. `created_at` is journaled, so FWD scoring only uses bars starting at or after it. |
| N7 | Replay time keys stripped UTC offsets without converting, putting 14:30Z bars at 14:30 ET (journal-universe look-ahead of 4–5 h). | A (bug) | **Fixed** and regression-tested (`_key` converts to America/New_York). |

## Multi-asset blockers found
- Replay hard-codes `"STK"` and a 0.01 tick.
- Shadow assumes USD/SMART and a 1-hour timeframe.
- The allocator has no contract multiplier.
- Costs are US-stock only.
- `PortfolioSnapshot` has no currency.
- The executor rejects non-STK (intended).

Each must be generalized before another asset class can feed decisions (see the
`market-discovery` skill).
