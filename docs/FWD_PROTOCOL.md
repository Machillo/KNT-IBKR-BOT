# Forward evaluation protocol FWD-v1 (pre-registered; hypotheses unchanged)

The three hypotheses were registered in `docs/experiments/LOG.md` on 2026-09-24 and are NOT
changed here. This document fixes HOW they are evaluated, before any forward result exists.
The evaluator is `research/fwd_protocol.py`, with tests in `tests/test_fwd_protocol.py`.
Changing any constant below is a new pre-registration (new id, new evidence window), never an
edit of this one. The version was revised once, after the quant-methodology review of
2026-09-24 and before any v2 forward row existed.

**A monthly return target (e.g. "5 %/month") is NOT a statistical criterion and plays no role
in any decision below.** A return number without a sample size, a benchmark, costs and a
multiple-testing correction cannot tell skill from luck or from market drift.

## Calendar segments (research/protocol.py)
- TRAIN: before 2023-09-01.
- VALIDATION: [2023-09-01, 2025-03-01).
- HOLDOUT: [2025-03-01, 2026-09-25). Still unused, and closed so that forward data can never
  fall inside it.
- FORWARD: from 2026-09-25. Only data recorded after registration.

## Evidence window and binding moment
- **Start:** the first market-open `shadow_only` cycle journaled with `DECISION_VERSION =
  selector_v1+decision_pipeline_v2`, scorer v3 and gates g1. That means after this code is
  merged and shadow-only is restarted from a clean checkout.
- **Binding cutoff:** the FIRST trading day on which every sample minimum of the test holds,
  or **2027-03-31**, whichever comes first.
  - Rows after the cutoff are never used. This rules out optional stopping: the evaluator
    finds the cutoff from the ordered data, whenever it is run.
  - Before the cutoff the output is `INCONCLUSIVE — monitoring only`, which is never a
    decision.
  - At the deadline without the minimum, the result is INCONCLUSIVE. Extending the window
    requires a new registration.
- **Validity:** the quality verdict (`research/shadow_quality.py`) is computed on exactly the
  cycles that produced the population. It must be `VALID_FOR_RESEARCH`:
  - no zero-tolerance gate in a market-open cycle;
  - ≤ 5 % of market-open cycles with a BLOCKING gate;
  - **one code version, a clean tree, one decision-config hash** across the window.
  Otherwise the result is INCONCLUSIVE.

## Population (FWD1, FWD2)
Every condition must hold:
- the decision is canonical (not a duplicate), in `shadow_only` mode, with learning frozen,
  and not a `CANDIDATE_ERROR`;
- the market was open at the cycle, and the cycle carries no BLOCKING gate;
- it was decided within 80 minutes of its decision bar completing. This excludes stale-bar
  re-decisions after restarts or overnight;
- it has a scorer-v3 5-bar forward return (status FINAL or PENDING_DATA). The 20-bar horizon
  is not required;
- its cycle has ≥ 5 such rows.

If more than **10 %** of the population rows lack a forward return, the result is
INCONCLUSIVE. Data missing not at random (halts, acquisitions) must not silently shape the
sample.

## Measure
- **Excess:** the side-adjusted GROSS 5-bar forward return minus the **leave-one-out** mean
  5-bar forward return of the other rows of the same cycle (the point-in-time cohort KNT's
  scanners offered).
- **Base price:** the forward return starts at the **open of the first bar that starts at or
  after the decision**. That is the first tradeable price; it is never the decision bar's
  close and never includes an overnight gap that preceded the decision. It runs exactly h
  bars from there.
- **Clustering and inference:**
  - Events are averaged per trading day (ET).
  - The standard error uses uniform (Hansen–Hodrick) weights at lag 1 day, because 5-bar
    windows spill into the next session. It is never below the i.i.d. standard error.
  - Critical values come from **Student t with days − 1 df**.
- **Costs:** 9.3 bps round trip (BASELINE). "Net" = daily excess − 0.093 %. This is
  conservative, because the benchmark leg carries no cost.

## Multiple testing
- **Family:** K = 3 (FWD1, FWD2, FWD3), α = 0.05 two-sided, Bonferroni.
- **KEEP threshold:** t ≥ t(1 − 0.05/6; days − 1). That is 2.50 at 40 days and 2.39 in the
  limit.
- This family is independent of the closed K = 15 validation family.

## FWD1: "Selected LONG setups are not better than the cohort" (replication of F6 on 1-hour bars)
- **Minimums:** ≥ 300 selected LONG events, ≥ 40 trading days and ≥ 60 symbols.
- **Decisions:**
  - **KEEP** (the selector carries positive information; contradicts F6): mean net > 0 and
    t(net) ≥ critical.
  - **REJECT** (hypothesis supported): the upper 97.5 % t-bound of the GROSS excess is below
    9.3 bps.
  - **INCONCLUSIVE:** anything else.

## FWD2: "NO_TRADE passes on setups as good as the ones it takes"
- **Measure:** per trading day, the mean excess of selected LONG events minus the mean excess
  of LONG counterfactuals (the best sub-threshold LONG evaluation of NO_TRADE rows). Only days
  with both are paired. SHORT counterfactuals are reported, never pooled.
- **Minimums:** ≥ 300 events in each arm and ≥ 40 paired days.
- **Decisions:**
  - **KEEP:** mean difference > 0 and t ≥ critical.
  - **REJECT** (hypothesis supported): the 95 % t-interval of the difference lies within
    ± 9.3 bps.
  - **INCONCLUSIVE:** anything else.

## Power (stated in advance, so no one is surprised by it)
The minimum detectable effect is about (t_crit + z_0.8) × sd_daily / √days. With a daily
excess standard deviation around 0.3–1 % and 40–150 days:
- KEEP needs a true net edge of roughly 20–60 bps per 5-bar trade;
- the REJECT branches (upper bound below 9.3 bps, or equivalence within ± 9.3 bps) are
  **unlikely to be reachable** unless the daily dispersion is small.

**INCONCLUSIVE is therefore the expected outcome of an honest window**, and it must be
reported as "not enough evidence either way", never as "no edge" or "edge". The observed
daily standard deviation is reported with every evaluation, so a follow-up registration can
size its window.

## FWD3: "12-1 cross-sectional momentum works on the point-in-time scanner universe"
- **Universe at each month-end:** every `ranked_eligible` name of the LAST market-open
  `shadow_only` cycle of the month. Use `JournalUniverse(run_mode="shadow_only", top_n=None)`;
  **no** 12-name cap, which exists only for per-cycle deep analysis. Require ≥ 30 members.
- **Bars:** `run_fetch_journal_bars.py` (merged history).
- **Formation:** 12-1 month return. Pre-journal bars may be used for formation only.
- **Measure:** equal-weight top quintile minus the universe mean, next-month return, net of
  9.3 bps × one-way turnover × 2. Monthly, non-overlapping.
- **Decision rule:** "same KEEP rule as round 3" (LOG.md), adapted only where a forward
  window cannot apply a TRAIN/two-profile step. KEEP requires ALL of the following:
  1. ≥ 12 monthly rebalances with ≥ 30 members each;
  2. mean net > 0 and t (uniform weights, lag 1 month; Student t, 12+ df) ≥ the K = 3
     critical value;
  3. both halves of the window positive. This replaces round 3's TRAIN and second-profile
     checks, which have no forward equivalent;
  4. it survives the destruction tests: cost × 3; entry delayed 1 trading day; formation
     window × 0.5 and × 1.5; removing the best 5 % of events; removing the best symbol.
- **Otherwise:**
  - REJECT when the upper 97.5 % bound of the mean net is below 0;
  - INCONCLUSIVE when fewer than 12 rebalances exist, or in any other case.
- **Timing:** not evaluable before the 12th month-end after the window start (≈ 2027-10).
  Its deadline is **2027-12-31**. The FWD3 evaluator must be written, reviewed and frozen
  before the 12th rebalance exists.

## Invalidation (the count restarts under a NEW registration)
Any of the following invalidates the window:
- a change inside the window of:
  - the code version (or a dirty tree);
  - the decision-config hash (risk, runtime and market-data settings);
  - `DECISION_VERSION`, the scorer version or the gates version;
- learning not frozen, or a non-zero selector bonus;
- a quality verdict of `INVALID_FOR_RESEARCH`;
- a hand-picked cutoff or data after the binding cutoff. The evaluator enforces this.

## Shadow → Paper plumbing criteria (plumbing only, NOT a strategy validation)
Paper plumbing tests the order round trip. It says nothing about edge. It may be scheduled
when ALL of these hold:
1. Shadow-only has run ≥ 10 trading days from the v2 code, and `run_shadow_report.py` reports
   **VALID_FOR_RESEARCH** for that window.
2. Every SHADOW_BLOCKED reason in those days is understood. There is no unexplained
   `decision_bar_time_missing`, `reference_not_live_market_data` or scanner failure in open
   sessions.
3. `run_paper_preflight.py` passes against the paper account (read-only;
   `docs/PAPER_PLUMBING_TEST.md`).
4. The kill-switch drill (`docs/KILL_SWITCH_DRILL.md`) has been run by a human in dry-run.
5. A human gives the ACK for the supervised session.

Moving from paper to **any** capital allocation requires all of these, and none exist today:
- FWD1 or FWD2 = KEEP;
- a frozen implementation;
- the one-time HOLDOUT test described in `docs/experiments/LOG.md`.
