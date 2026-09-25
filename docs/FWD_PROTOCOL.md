# Forward evaluation protocol FWD-v1 (pre-registered; hypotheses unchanged)

The three hypotheses were registered in `docs/experiments/LOG.md` on 2026-09-24 and are NOT
changed here. This document fixes HOW they are evaluated, before any forward result exists.
The evaluator is `research/fwd_protocol.py`, with tests in `tests/test_fwd_protocol.py`.
Changing any constant below is a new pre-registration (new id, new evidence window), never an
edit of this one. The version was revised three times: r1 and r2 after the quant-methodology
reviews of 2026-09-24, and r3 after the round-4 reviews of 2026-09-25. All three came before
any window was registered and before any v2 forward row existed; no pre-registration shadow
row was looked at. r3 adds registration integrity, blinding, per-arm missingness, honest FWD2
labelling, the instrument-type universe and the deployment recipe.

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
- **Start:** REGISTERED once by the shadow-only process itself, at its first start from the
  pinned checkout: `python run_shadow_only.py --register-fwd-window` (see §Pinned deployment).
  The fingerprint and config hash are therefore the ones actually running.
  - The registration file (`<journal>.fwd_window.json`) pins five things:
    - the start time (it cannot be backdated);
    - the registration time;
    - the **decision-code fingerprint** (a content hash of every file on the decision path,
      the cost model included);
    - the decision-config hash;
    - the **protocol fingerprint** (evaluator, scorer, quality gates, protocol calendar, cost
      model).
    It is never overwritten.
  - Commits that do not touch the decision path (docs, scorer, reports) do not change the
    fingerprint. Any change to the decision path does, and invalidates the window.
  - Rows before the registered start never count.
  - **Tamper evidence (guards against accidents; makes cheating deliberate, cannot prevent
    it):** registration prints one line. Commit it DIRECTLY to `main`, push it within
    **3 days**, and never squash, rebase or re-wrap it. The evaluator reads the git history of
    every local ref and of `origin/main`. Lines are parsed as records (markdown and extra spaces
    are tolerated) and deduplicated by `start_utc`. It refuses:
    - a registration line that was never committed, is not reachable from `origin/main`, or was
      committed outside that delay;
    - a registration line that no longer matches the registration file;
    - **more than one `FWD-v1` registration ever committed**, even one that was later deleted.
      A new window needs a new protocol id and counts in the family.
    - evaluator, scorer or gates code that differs from the protocol fingerprint;
    - shadow-only cycles of the registered decision code more than 1 hour before the start
      (a pre-registration look).
  - **Git limitations (stated):**
    - committer dates can be forged;
    - rewrites of UNPUSHED commits leave no trace;
    - a pre-registration look run in ANOTHER state directory is invisible.
    These rules are procedural.
  - **Registration preconditions** (`run_shadow_only.py --register-fwd-window`, applied after a
    successful start):
    - `MAX_TRADE_RISK_PCT ≤ 0.01` and `MARKET_DATA_TYPE = 1`;
    - `--risk-limits-reviewed`: an explicit acknowledgement of the pinned daily-loss, drawdown
      and position limits;
    - `KNT_STATE_DIR` and `KNT_BOT_STATE_DIR` set, and the bot directory existing;
    - a FRESH journal with no cycle of this code. Smoke tests use a different `KNT_STATE_DIR`,
      never the FWD one.
  - **Ending a window:** `run_shadow_only.py --end-fwd-window` appends an end record (never
    deletes it) and logs it at CRITICAL.
    - Rows after the end never count.
    - A window that ended BEFORE its binding cutoff never binds. Its interim state, without
      outcome statistics, must be reported in LOG.md, and it counts in the family (a later
      FWD-v2 counts v1's tests in its K).
    - A window that had ALREADY bound keeps that result.
  - **Binding results are stored:** the first binding result of each test is written into the
    registration file and returned ever after. It can never be re-evaluated away.
  - **Scores carry their rules:** every outcome row records the protocol fingerprint of the code
    that scored it, and its provider. Rows scored under other rules (e.g. from the development
    checkout) block binding until they are rescored from the pinned checkout. The first forward
    returns written are frozen (no provider shopping); the trade-off is that a bad first value
    cannot be corrected.
  - **FWD1 → FWD2 information:** if FWD1 binds first, its unblinded selected arm is also FWD2's
    selected arm. This is declared here. FWD2's definitions and cutoff are mechanical and
    pinned, so seeing that arm cannot change FWD2's rules.
  - **Blinding:** until the binding moment the evaluator returns NO outcome statistic (means,
    t, missing shares, breakdowns print as `BLINDED`). `run_score_shadow.py` prints counts only
    unless `--unblind`, and every unblinded run is an interim look that must be recorded in
    LOG.md.
- **Binding cutoff:** the FIRST trading day, never after **2027-03-31**, on which every sample
  minimum of the test holds, or the deadline.
  - The minimums are counted from the **decisions** themselves, after the per-cycle quality
    exclusions. They never depend on how far the scorer has got.
  - The result becomes binding only once every row up to the cutoff has been scored.
  - Rows after the cutoff are never used. This rules out optional stopping: the evaluator
    finds the cutoff from the ordered data, whenever it is run.
  - Before the cutoff the output is `INCONCLUSIVE — monitoring only`, which is never a
    decision.
  - At the deadline without the minimum, the result is INCONCLUSIVE. Extending the window
    requires a new registration.
- **Validity:** the quality verdict (`research/shadow_quality.py`) is computed on every cycle
  from the registered start to the cutoff, excluded cycles included. It must be
  `VALID_FOR_RESEARCH`:
  - no zero-tolerance gate in a market-open cycle;
  - ≤ 5 % of market-open cycles with a BLOCKING gate;
  - **one decision-code fingerprint, equal to the registered one, and one decision-config
    hash** across the window.
  Otherwise the result is INCONCLUSIVE.

## Population (FWD1, FWD2)
Every condition must hold:
- the decision is canonical (not a duplicate), in `shadow_only` mode, with learning frozen,
  and neither a `CANDIDATE_ERROR` nor an `INSTRUMENT_EXCLUDED` row;
- **universe:** US common stocks, ADRs and REITs as typed by IBKR (`stockType`). ETFs
  (leveraged and inverse included), ETNs and unknown types are excluded BEFORE the decision.
  - Deep analysis takes the first 12 ELIGIBLE names (liquidity order) whose type passes; excluded
    types do not use up a slot (at most 36 type lookups per cycle).
  - A FAILED type lookup is a `CANDIDATE_ERROR` (`metadata_unavailable`), never an exclusion. A
    cycle with more than 5 % candidate errors is BLOCKING;
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
  of LONG counterfactuals. Only days with both are paired. SHORT counterfactuals are reported,
  never pooled.
- **Counterfactual (pinned before registration, matching the code):** a NO_TRADE decision
  whose BEST non-flat evaluation — the highest adjusted score, i.e. the journal's `top_*`
  columns — is LONG and has a stop and a target.
  - A NO_TRADE row whose best evaluation is SHORT is **not** a LONG counterfactual, even if a
    LONG evaluation ranks below it.
  - The per-strategy records in `shadow_opportunities` are never used to redefine the
    counterfactual.
- **What it tests in practice (stated before registration):** the TRAIN census
  (docs/ARCHITECTURE_TARGET.md §2) shows that most NO_TRADE decisions come from the
  high-volatility pause, not from the score threshold. FWD2 therefore tests NO_TRADE as it
  actually occurs, mostly the pause. The two arms come from different regimes, so the test is
  **regime-confounded by construction**. A KEEP or REJECT is a statement about the NO_TRADE
  rule as a whole, never about the score threshold alone.
- **Pre-registered descriptive breakdown (never tested):** at the binding moment, the
  counterfactual arm is reported by NO_TRADE reason (`no_trade_reasons`).
- **Minimums:** ≥ 300 events in each arm and ≥ 40 paired days.
- **Missing data:** the 10 % limit applies to each arm AND to all population rows (the
  cohort benchmark).
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
  `shadow_only` cycle of the month whose latest recorded IBKR type (`instrument_types`, at or
  before that cycle) is COMMON/ADR/REIT.
  - Names never looked up are excluded. That is a stated bias towards the most liquid names.
  - There is no 12-name cap; it exists only for per-cycle deep analysis.
  - Require ≥ 30 members, otherwise that rebalance does not count.
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
     Round 3 varied the HOLDING horizon. Monthly momentum has a fixed 1-month holding
     period, so the formation window is varied instead. This is a stated CHANGE, not a
     silent adaptation.
- **Otherwise:**
  - REJECT when the upper 97.5 % bound of the mean net is below 0;
  - INCONCLUSIVE when fewer than 12 rebalances exist, or in any other case.
- **Timing:** not evaluable before the 12th month-end after the window start (≈ 2027-10).
  Its deadline is **2027-12-31**. The FWD3 evaluator must be written, reviewed and frozen
  before the 12th rebalance exists.

## Exploratory records (`shadow_opportunities`) — governance
Shadow-only also journals EVERY strategy evaluation of every decision (engine/opportunity.py).
These records are **exploratory, not FWD evidence**:
1. The FWD evaluator, the scorer and the quality gates never read them. A test enforces this
   for those modules; it is a code convention, not a database permission.
2. **Embargo.** This is a procedural commitment, not a technical lock: the records sit in the
   same SQLite file. No per-strategy or per-context analysis of forward data happens until
   both FWD1 and FWD2 reach their binding cutoff (or 2027-03-31); until then the records are
   only recorded. The same applies to the unblinded scorer report (`--unblind`): its
   per-regime and FWD-style statistics are interim looks.
3. After the embargo, every exploratory query is written down in `docs/experiments/LOG.md`,
   so the number of looks K is counted. Any resulting hypothesis gets a NEW registration and is
   tested only on data recorded after that registration.
4. Nothing derived from them — a selector bonus, a lifecycle promotion, an admission filter —
   may change the decision path while any FWD window is open.
5. Strategy × context exploration uses **TRAIN only**. VALIDATION is spent (15 tests;
   LOG.md). The replay does not journal opportunities yet (roadmap PR-B), so this is a design
   commitment, not a working tool.

## Pinned deployment (exact recipe)
The window's evidence comes from ONE immutable checkout. State and `.env` are anchored to each
checkout, so the recipe makes them explicit.
1. **Create the pinned worktree** at the reviewed commit, and copy the reviewed `.env` into it
   (`.env` is untracked):
   ```bash
   git worktree add ../knt-fwd <commit>
   ```
   In that `.env`: `MARKET_DATA_TYPE=1`, `MAX_TRADE_RISK_PCT=0.01`, no ACK variables.
2. **Point every process at the same absolute state directories** (environment variables, set
   the same way in every shell that touches the window):
   - `KNT_STATE_DIR=<absolute path>`: the shared FWD state, holding the journal, the
     registration file and the scores;
   - `KNT_BOT_STATE_DIR=<absolute path of the trading bot's state/>`: read-only, used to mirror
     the bot's sticky kill and drawdown lock.
3. **Start and register, from the worktree:**
   ```bash
   python run_shadow_only.py --register-fwd-window --risk-limits-reviewed
   ```
   Registration refuses unless `MAX_TRADE_RISK_PCT <= 0.01` and `MARKET_DATA_TYPE = 1`.
4. **Commit the printed line** to `docs/experiments/LOG.md` on the development checkout (main
   branch), within 3 days. Git history is shared by all worktrees.
5. **Score and report from the worktree**, with the same `KNT_STATE_DIR`:
   ```bash
   python run_fetch_journal_bars.py
   ```
   ```bash
   python run_score_shadow.py
   ```
   ```bash
   python run_shadow_report.py --persist
   ```
   Output shows counts only.
6. **Evaluate from the worktree too** (`python run_shadow_report.py --fwd`). The evaluator code
   is pinned by the protocol fingerprint; it finds the committed line through the shared git
   history.

Development continues on other branches; later commits never touch the running process.
`run_shadow_only.py` refuses to (re)start when a registered window exists and the running
decision code or config differs from it; a corrupt registration file is refused too. Only
`--end-fwd-window` overrides this, and that ENDS the window (recorded, reported, counted).
Safety fixes to the decision path found during the window go to the development branch; the
pinned process keeps running until the window binds or is explicitly ended.

## Forward replays
`run_pipeline_backtest.py --segment forward` is refused while a registered window is open and
FWD1/FWD2 have not both bound. It prints interim statistics, so it would be an unblinded look.
After that it requires `--confirm-forward`. Comparing variants
on forward data is selection that uses up the FWD evidence. Only a variant registered in
`docs/experiments/LOG.md` may be run there.

## Scoring source
Forward rows are scored from `reports/pit_cache` (the default of `run_score_shadow.py`) or
from IBKR. Only the IBKR provider may expire an unalignable row to NOT_EVALUABLE; a cache that
lacks the bars leaves the row pending.

## Invalidation (the count restarts under a NEW registration)
The decision-code fingerprint deliberately covers ALL of `engine/`, `execution/`, `market/`,
`portfolio/`, `risk/`, `strategies/`, `core/`, `config/config.py`,
`research/shadow_journal.py` and `run_shadow_only.py`. It is conservative: even a harmless
edit there (for example a log line), once deployed and restarted, invalidates FWD1/FWD2.
**Do not patch or restart the shadow-only process on changed code during the window.**
`research/learning.py` and `research/performance.py` are not covered. That is safe only
because learning is frozen in shadow-only, and the gates enforce it (`learning_drift`).

Any of the following invalidates the window:
- a change inside the window of:
  - the decision-code fingerprint (any decision-path file, modified or untracked);
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
