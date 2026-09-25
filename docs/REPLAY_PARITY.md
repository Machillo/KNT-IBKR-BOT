# Runtime vs replay parity

Shared code, used by runtime, order-free shadow and replay:
- `engine/decision.DecisionPipeline`: `StrategySelector` → `PortfolioAllocator` →
  `PortfolioAdmissionCoordinator` (RiskManager, PortfolioBrain, CrossExposureGuard);
- `engine/decision.DECISION_CONTEXT_BARS`: the selector sees exactly the same number of
  completed bars;
- `execution/pretrade.evaluate`: the executor's pure pre-transmission checks, in the
  executor's order (session → lock → sanity/finite numbers → side → short policy → daily cap
  → decision-bar freshness → instrument/quantity → tick normalization → geometry → fresh
  reference);
- `execution/pretrade.hard_risk_refusal`: the hard-risk re-check on tick-rounded prices.

These are asserted in `tests/test_replay_parity.py`, `tests/test_pretrade.py` and
`tests/test_shadow_research_path.py`. The replay is `PIPELINE_VERSION = 4`. Results from
v3 or earlier are not comparable with v4.

`PipelineResult.runtime_equivalent` is True only when BOTH of these hold:
- every decision-shaping knob equals the runtime default (threshold, strategies, caps,
  limits, entry mode, context, research filters);
- the data is 1-hour bars.

Daily and 4-hour replays are research variants. The runtime never decides on them: their
decision bar is 17–24 h old at submission, and the executor refuses such bars as
`stale_decision_bar`.

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
| 3 | Bar availability: the runtime calls a bar complete at start + size. | A→D | Documented. | **Equivalent except the first RTH bar.** Replay places the order at bar *i+1*, available at the end of *i*, and keys are exchange time (N7). IBKR's first 1 h RTH bar is labelled 09:30 and covers 09:30–10:00. The runtime (start + 1 h) only uses it from 10:30, while replay can act on it at 10:00 and fill from the 10:00 open. Bias: replay optimistic by up to 30 min on the first bar of each day. To verify against IBKR and fix with the session calendar. |
| 4 | The runtime selector had a learning store (bonus/AVOID); replay has none. | B/D | Learning drifted inside the forward window. | **Eliminated for shadow-only.** Learning is frozen (`learning_enabled=False`) and `selector_bonus`/`learning_mode` are journaled. `paper_alpha` still learns (runtime choice). |
| 5 | Sector metadata is required at runtime; replay has none, so the sector cap never binds. | B | Replay optimistic. | Unchanged. Sector is journaled per decision from now on (v2). Needs point-in-time sectors (docs/POINT_IN_TIME_DATA.md). |
| 6 | Runtime pending exposure includes the stop/target children of filled brackets. | A (loosens a gate) | Runtime more conservative. | **Kept on purpose.** Removing it would relax a live gate. Bias: replay slightly optimistic. |
| 7 | The executor's fresh-quote check (≤ 1.5 %, live data type 1) has no replay equivalent. | C | Silently skipped. | **Explicit.** Replay runs pretrade with `reference_available=False`. Every submission without it is counted in `PipelineResult.reference_unchecked`. Shadow-only runs the check on a real read-only quote. Quote history would make it B. |
| 8 | Session buffers (first 5 / last 15 min, half-days, holidays). | A | Replay only refused last-bar decisions. | **Equivalent for 1h RTH bars.** Decisions become available at XX:00. The one at the close is refused (next bar on another date), half-day closes too, and no XX:00 falls inside a buffer. The check goes through `pretrade` (`market_session_closed`). |
| 9 | The daily cap counts journal INTENT rows per UTC date, including failures; replay counts submissions. | C | Slightly optimistic. | Equivalent except transmission failures, which cannot be simulated. RTH never crosses a UTC date. |
| 10 | Sticky multi-day drawdown lock. | A | Missing in replay (optimistic). | **Eliminated.** Replay tracks the high-water mark of marked equity and locks for good at `max_drawdown_pct`. Remaining difference: replay marks per bar, the supervisor per poll (C). |
| 11 | The runtime takes its limits from `.env`; replay used `PipelineConfig` defaults. | A | Could drift. | **Eliminated on demand.** `PipelineConfig.from_bot_config` and `run_pipeline_backtest.py --config-from-env`. The defaults still equal the code defaults. |
| 12 | The runtime analyses the top-12 eligible names by liquidity; replay analysed every member. | A (journal) / B (CSV) | Optimistic. | **Eliminated for the journal universe.** `JournalUniverse(top_n=SHADOW_MAX_CANDIDATES, run_mode="shadow_only")` keeps the same liquidity order (ties by scanner rank). CSV universes stay B. Remaining difference: replay reads membership at the bar START, one hour before the runtime decides. This is conservative (no look-ahead). |
| 13 | The entry parent uses the IBKR Adaptive algo; replay fills exactly at the limit, with a strict trade-through. | C | Minor. | Unchanged. Run cost/slippage stress (`--cost stressed` / `severe`) and do not treat touch fills as queue-guaranteed. |

## Additional divergences found by the architecture review of this round

| # | Divergence | Class | After |
|---|---|---|---|
| N1 | Shadow-only and unarmed `paper_alpha` did not accumulate same-cycle approvals. | A | **Eliminated for shadow-only.** SHADOW_SUBMIT entries become pending exposure for the rest of the cycle *and* of the UTC day (virtual book). The book is conservative: a would-be DAY order occupies capacity all day, even if it would have filled and exited, and it is gone the next day, so overnight positions are not modelled (D). Unarmed `paper_alpha` still does not accumulate (only shadow-only is the evidence source). |
| N2 | Shadow-only skipped every executor check. | A | **Eliminated.** Pretrade + hard risk + daily cap + duplicate check on a fresh read-only quote give `SHADOW_SUBMIT` or `SHADOW_BLOCKED:<reason>`. Broker-only checks (paper guard, connection, broker equity) cannot run in a read-only session. |
| N3 | Shadow-only has its own drawdown high-water mark (state/shadow_only). | C | Documented. The research lock also mirrors the trading bot's persisted sticky kill and drawdown lock, read-only. |
| N4 | Research filters bypass the pipeline. | A | **Flagged.** `PipelineResult.runtime_equivalent` is False for any research-only knob. |
| N5 | Tick rounding came before the executor's risk re-check but not in replay. | A | **Eliminated.** Replay re-checks hard risk on the rounded prices through `pretrade.hard_risk_refusal`. |
| N6 | Re-decision cadence: the runtime decides up to `SHADOW_INTERVAL_SECONDS` after a bar completes, and re-decides the same completed bar about 4 times per hour; replay decides once, at completion, and may fill at the next bar's open. | C/D | Documented. Bias: replay optimistic on open-of-bar fills. After a rejection within the hour, the runtime can approve the same bar later; replay cannot. Scorer v3 measures fills and forward returns only from the first bar starting at or after `created_at`. |
| N7 | Replay time keys stripped UTC offsets without converting, putting 14:30Z bars at 14:30 ET (journal-universe look-ahead of 4–5 h). | A (bug) | **Fixed** and regression-tested (`_key` converts to America/New_York). |
| N8 | Candidate processing order: the runtime meets candidates in liquidity order and the first approved takes capacity and the daily cap; replay sorted by selector score. | A | **Eliminated.** Replay uses the journal universe's liquidity rank (`rank_at`) and otherwise trailing 20-bar dollar volume, never the score. |
| N9 | Decision-bar freshness: the executor refuses bars completed more than 75 min earlier (`stale_decision_bar`). At the open this would otherwise trade the previous session's last bar. | A (runtime fix) | Runtime, shadow-only and quality gates enforce it. Replay passes `freshness_checked=False`. That is valid only for 1-hour data, where orders are placed at completion; for daily/4h data it is false, so those runs are `runtime_equivalent=False`. |
| N10 | Context warm-up: replay decided with fewer than 140 bars at the start of the data. | A | **Eliminated.** Such decisions are skipped and counted in `warmup_skipped`. |
| N11 | Virtual book after a restart of shadow-only. | A | **Eliminated.** Today's SHADOW_SUBMIT rows are reloaded from the journal. |
| N12 | Shadow-only does not simulate the P&L of its would-be trades. Equity, daily loss, allocator budget and the drawdown lock come from the REAL (paper) account, so they never react to shadow outcomes, whereas they do in replay and runtime. | D | Documented. Bias: shadow sizing and locks ignore its own hypothetical losses. If `paper_alpha` trades the same account, shadow-only sees its positions and orders as exposure. |

## Multi-asset blockers found
- Replay hard-codes `"STK"`. The shared pretrade module enforces STK, whole shares and a
  0.01 tick for shadow-only and replay too, not just the executor.
- 1-hour assumptions: `_bar_completed_at` (1 hour), `max_bar_age_seconds = 4500`, the
  journal's `timeframe = "1 hour"`, and shadow's session check requires STK.
- Broker equity accepts only BASE/USD.
- Shadow assumes USD/SMART and a 1-hour timeframe.
- The allocator has no contract multiplier.
- Costs are US-stock only.
- `PortfolioSnapshot` has no currency.
- The executor rejects non-STK (intended).

Each must be generalized before another asset class can feed decisions (see the
`market-discovery` skill).
