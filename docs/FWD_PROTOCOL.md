# Forward evaluation protocol FWD-v1 (pre-registered; hypotheses unchanged)

The three hypotheses were registered in `docs/experiments/LOG.md` on 2026-09-24 and are NOT
changed here. This document fixes HOW they are evaluated, before any forward result exists.
The evaluator lives in `research/fwd_protocol.py`, with tests in
`tests/test_fwd_protocol.py`. Any change to a constant below is a new pre-registration
(new id and a new evidence window), never an edit of this one.

**A monthly return target (e.g. "5 %/month") is NOT a statistical criterion and plays no role
in any decision below.** A return number without a sample size, a benchmark, costs and a
multiple-testing correction cannot distinguish skill from luck or from market drift.

## Evidence window
- **Start:** the first shadow-only cycle recorded with `DECISION_VERSION =
  selector_v1+decision_pipeline_v2`, scorer v2 and quality gates g1. That means after this
  code is merged and shadow-only is restarted from it. Earlier rows (v1) are excluded; the
  gates mark them `legacy_decision_version`.
- **Source:** `state/shadow_only/strategy_performance.db`, `run_mode = shadow_only`, learning
  frozen.
- **End:** evaluate ONCE, at the first of these two moments:
  - the minimum sample is reached;
  - **2027-03-31**.
  If the minimum is not reached by the deadline, the result is INCONCLUSIVE. The window is
  never extended silently: extending it is a new registration.

## Population (FWD1, FWD2)
Every condition must hold:
- the decision is canonical (not a duplicate) and not a `CANDIDATE_ERROR`;
- it is scored by scorer v2 with status FINAL and a 5-bar forward return;
- the market was open at the cycle;
- its cycle passed every BLOCKING quality gate;
- its cycle has at least 5 scored rows (the leave-one-out benchmark needs a cohort).

## Measure
- **Excess** = side-adjusted GROSS 5-bar forward return minus the **leave-one-out** mean
  5-bar forward return of the other scored rows of the same cycle. This is the point-in-time
  cohort benchmark: the names KNT's scanners offered at that moment.
- **Clustering and inference:** events are averaged per trading day (ET date of the
  decision). The t-statistic is Newey–West over the daily series with lag 1 day, because
  5-bar windows spill into the next session. Intra-day cycles overlap heavily, so
  per-event or per-cycle t would be optimistic.
- **Costs:** 9.3 bps round trip (BASELINE model). "Net" = daily excess − 0.093 %. This is
  conservative, because the benchmark leg carries no cost. Cost ×3 is reported, not decided on.

## Multiple testing
The forward family is K = 3 (FWD1, FWD2, FWD3), at α = 0.05 two-sided with Bonferroni. That
gives **|t| ≥ 2.40**. It is independent of the K = 15 validation family, which is closed.

## FWD1: "Selected LONG setups are not better than the cohort" (replication of F6 on 1-hour bars)
- **Sample minimum:** ≥ 300 selected LONG events, ≥ 40 trading days and ≥ 60 distinct symbols.
- **Decisions:**
  - **KEEP** (the selector carries positive information; contradicts F6): mean net excess > 0
    AND NW t of the net excess ≥ 2.40.
  - **REJECT** (the hypothesis is supported: no usable information): the upper 97.5 % bound
    of the GROSS excess is below the 9.3 bps cost.
  - **INCONCLUSIVE:** anything else, including an insufficient sample or an invalid window.

## FWD2: "NO_TRADE passes on setups as good as the ones it takes"
- **Measure:** per trading day, the mean excess of selected LONG events minus the mean
  excess of LONG counterfactuals (the best sub-threshold LONG evaluation of NO_TRADE rows).
  Only days that have both are paired. SHORT counterfactuals are reported separately, never
  pooled.
- **Sample minimum:** ≥ 300 events in each arm and ≥ 40 paired days.
- **Decisions:**
  - **KEEP** (the threshold separates better setups): mean difference > 0 AND NW t ≥ 2.40.
  - **REJECT** (the hypothesis is supported): the 95 % interval of the difference lies
    within ± 9.3 bps.
  - **INCONCLUSIVE:** anything else.

## FWD3: "12-1 cross-sectional momentum works on the point-in-time scanner universe"
- **Universe:** month-end membership from `JournalUniverse` (UTC-correct keys, see
  REPLAY_PARITY N7).
- **Bars:** `run_fetch_journal_bars.py` (merged history).
- **Formation:** 12-1 month return. Bars from BEFORE the journal started may be used for
  formation only; membership must be journal-based.
- **Measure:** equal-weight top quintile minus the universe mean, next-month return, net of
  9.3 bps × one-way turnover × 2. Monthly, non-overlapping. NW lag 1 month, as a conservative
  choice.
- **Sample minimum:** ≥ 12 monthly rebalances with ≥ 30 members each. Realistically this is
  not evaluable before the 12th month-end after the evidence window starts (about 2027-10).
  Its deadline is therefore **2027-12-31**, not the FWD1/FWD2 date.
- **Decisions:**
  - **KEEP:** mean net > 0 AND t ≥ 2.40.
  - **REJECT:** upper 97.5 % bound < 0.
  - **INCONCLUSIVE:** anything else.
- The FWD3 evaluator is not implemented yet; it cannot run on fewer than 12 months of data.
  It must be written and reviewed before the first 12 rebalances exist, then frozen.

## Invalidation (the count restarts under a NEW registration)
Any of these invalidates the window:
- a change to `DECISION_VERSION`, selector parameters, strategy code, the pretrade checks,
  the scorer version or the gates version;
- learning not frozen, or a non-zero selector bonus;
- the session verdict `INVALID_FOR_RESEARCH`: a zero-tolerance gate, or more than 5 % of
  cycles with a BLOCKING gate;
- evaluating before the minimum sample or the deadline ("peeking"). The evaluator may be
  run for monitoring, but its decision is only binding at the registered moment.

## Shadow → Paper plumbing criteria (plumbing only, NOT a strategy validation)
Paper plumbing tests that orders round-trip correctly. It says nothing about edge. It may be
scheduled when ALL of these hold:
1. Shadow-only has run ≥ 10 trading days from the v2 code, and `run_shadow_report.py`
   reports **VALID_FOR_RESEARCH** with zero zero-tolerance gates.
2. Every SHADOW_BLOCKED reason in those days is understood: no unexplained
   `decision_bar_time_missing` or `reference_not_live_market_data`, and no scanner failures
   left unaddressed.
3. `run_paper_preflight.py` passes against the paper account (read-only; see
   `docs/PAPER_PLUMBING_TEST.md`).
4. The kill-switch paper drill (`docs/KILL_SWITCH_DRILL.md`) has been run by a human in dry-run.
5. A human gives the ACK for the supervised session.

Moving from paper to **any** capital allocation requires FWD1 or FWD2 to be KEEP, a frozen
implementation, and the HOLDOUT test described in `docs/experiments/LOG.md`. None of those
exist today.
