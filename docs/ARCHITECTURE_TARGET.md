# KNT target architecture: audit, gaps, design and roadmap

Status 2026-09-25, branch `claude/overnight-session2` (draft PR #3). This document fixes the
design BEFORE building it. Everything here that is not listed under "Implemented in PR #3" is
design only.

**The FWD-v1 window is NOT registered.** Nothing below may change the decision path of a
registered window (docs/FWD_PROTOCOL.md §Pinned deployment).

## 1. Target pipeline
```
Markets (IBKR, per asset class, only if its InstrumentSpec is complete)
 → Discovery (scanners/universe, point-in-time, journaled funnel)
 → Context / regime (versioned detector, same context length everywhere)
 → Compatible strategies (lifecycle status ∈ SELECTABLE; asset class supported by the strategy)
 → Candidate opportunities (one per instrument × strategy × direction; engine/opportunity.py)
 → Feasibility (instrument spec, capital, whole units, cost in R, session, data quality)
 → Comparison (ONLY on calibrated, forward-validated expected R net of cost; else NO TRADE)
 → Portfolio allocation (greedy under constraints, cycle-wide, not first-come)
 → Risk (hard veto: RiskManager, PortfolioBrain, CrossExposureGuard, locks) — can veto anything
 → Execution (pretrade → paper guard → bracket; never re-decides the strategy)
 → Journal + scoring + quality gates (evidence for the NEXT registered test, never the current one)
```
NO TRADE is a first-class outcome at every arrow.

## 2. Audit map (what exists, traced end to end)

| Component | Status | Evidence / note |
|---|---|---|
| Discovery (IBKR scanners, funnel) | Partial, STK-coupled | Only `STK.US.MAJOR` plans; non-STK dropped before quoting (`market/intelligence.py`); ETFs pass as stocks, with no leverage or inverse handling. |
| Liquidity ranking | Partial | Score = 100 − 2·rank − 0.5·spread bps; volume unused; $1 minimum price; stock-only notion of liquidity. |
| Regime / context | Exists, heuristic | Hardcoded thresholds (`market/regime.py`). EMA50 at runtime (140 bars) vs EMA200 in walk-forward research (450 bars), so research and runtime labels differ. |
| Strategy registry | Exists | 10 single-asset strategies (`strategies/library.py`, `confluence.py`). Pairs are computed and then discarded (not in the registry). |
| Strategy lifecycle | **Implemented now** | `strategies/lifecycle.py`: all 10 are SHADOW; autonomous paper needs PAPER. |
| Compatible-strategy filter | Missing | Every strategy runs on every symbol; regime acts only through additive bonuses. |
| Selector | Exists, **methodologically not valid** | Argmax over heuristic 0–100 scores with incomparable scales. F6: REJECT (negative). Census below. |
| Candidate opportunities | **Implemented now (recording)** | `engine/opportunity.py`, one per evaluation, journaled to `shadow_opportunities`. |
| Expected return / utility | Missing | None anywhere; `Opportunity.expected_return_pct` is None by design. |
| Cross-opportunity comparison | Missing | Per-symbol argmax, then first-come capacity in liquidity order (runtime and replay). |
| Transaction-cost awareness | Partial | Costs only in replay and scoring. Decisions ignore cost; now recorded as `cost_in_r`. |
| Position sizing | Exists, STK-only | Size = min(1 % risk, 10 % notional), floored to whole shares; sized on tick-rounded prices. No multiplier, no margin. |
| Capital allocation | Partial | Independent per trade. No feasible set, no ranking, no minimum-capital logic. |
| Portfolio risk | Exists (single-opportunity checks) | Gross 80 %, cash reserve net of pending orders, daily loss, drawdown, sector 30 %, correlated cluster 40 % (pairwise 0.80, ≥ 20 returns), \|corr\| > 0.85 (≥ 40 returns). GTC exits count as pending exposure across days (effective cap ≈ 3 positions). No total open-risk ("heat") limit, no margin, no issuer map. |
| Risk veto | Exists and correct | RiskManager sits on every approval path, and nothing overrides it. |
| Learning / performance tracking | Exists, **not valid** | Thresholds unjustified. Research windows overlap the holdout. Regime labels mismatched. **Now OFF by default everywhere.** |
| Shadow learning / evidence | Exists | Journal v2 + opportunities, scorer v3, quality gates, FWD-v1. |
| Replay | Exists (v4) | Parity documented in REPLAY_PARITY. Sector cap not enforced in replay (no point-in-time sectors). |
| Point-in-time data | Partial | Forward journal plus a spec for external data. History is survivorship-biased (38 names). |
| Execution | Exists, STK-only | **Fixed now:** bracket children were DAY. **Fixed now:** kill switch no longer market-flattens non-STK. |
| Contract abstraction | Missing | No InstrumentSpec. `multiplier`, `minTick`, market rules, margin and `whatIf` are never read. |
| Session / calendar | STK-coupled | One global US-equity session from SPY `liquidHours`; ET dates hardcoded; 1 h timeframe hardcoded. |

### Selector census (descriptive, TRAIN only, no returns looked at)
`tools/selector_census.py`, run 2026-09-25:
- **Data:** the 38-name cache, bars before 2023-09-01, 140-bar context, learning off.
- **Sampling:** every 5th bar per symbol, so evaluations are autocorrelated.
- **Timeframe:** 4h and daily bars only. The runtime uses 1-hour bars, but the 1h cache lies in
  VALIDATION/HOLDOUT and was deliberately not used, so **the 1h rates are unknown**.
- **Bias:** the cohort was chosen with hindsight and over-represents trending names, which
  inflates momentum_gap's share.

Findings:
- The selector chooses a trade in **86 % (4h) / 76 % (daily)** of evaluations.
- NO_TRADE comes mostly from the high-volatility pause: 71 % of NO_TRADE on 4h (20 %
  "all strategies flat"), 87 % on daily. The threshold of 55 binds in ~1 % of evaluations.
- **momentum_gap_v1 is selected 51 % / 57 % of the time, including 28–34 % in RANGE.**
  momentum_gap + trend_following + swing_structure together take ~93 %. swing_structure's score
  is the constant 70.

On these data it behaves as a trend/momentum basket behind a selector, not evidence-based
multi-strategy selection. It is not changed now: FWD1 is testing exactly this selector, and
changing it would change the object under test. Replacing it is PR-E below.

## 3. Quantitative defects in the requested architecture, and the alternative
1. **"Compare opportunity A+X with B+Y" needs a common currency.** Heuristic scores have
   different formulas and scales, so ranking by them is arbitrary.
   - Alternative: compare only on a **calibrated expected R net of cost** (see §5), with its
     uncertainty.
   - Until a strategy has a calibration fitted on forward data, its opportunities can be
     recorded but not ranked against others. The rational outcome is then NO TRADE, or
     feasibility-only paper plumbing.
2. **"Capital-agnostic" cannot mean "always trades".**
   - At $1,000, a $20 stock with a 2 % stop costs about 1R round trip in IBKR minimum
     commissions, and any stock above $100 cannot be bought within the 10 % notional cap.
   - Alternative: an explicit **feasible set**. An opportunity is feasible only if:
     - whole units fit the caps;
     - the full cost (with commission at the proposed size) is ≤ a registered fraction of R;
     - the account rules allow it (PDT, settlement).
   - Small accounts then legitimately end with NO TRADE rather than forced tiny trades.
3. **Evidence per strategy × asset class × regime × horizon explodes into cells.** About 40
   cells per asset class and horizon. Power needs about 200–400 trades per cell to detect
   0.2R after correction.
   - Alternative: hierarchical partial pooling. Use regime-specific evidence only when the
     between-regime heterogeneity τ is estimated > 0. Never at symbol level.
4. **The lifecycle idea is sound only if each promotion is a pre-registered test**, with its
   own α, on data recorded after registration. Promotion by an online metric crossing a
   threshold is optional stopping and must not exist.
5. **"Regime X → strategy Y"** is avoided, but the current regime bonuses are hand-set and
   unvalidated. The compatibility filter must itself be evidence (see §5), not a new table of
   guesses.

## 4. Three layers and their boundary
- **Research** (`research/*`, `backtest/*`, offline runners, exploratory journal tables):
  - proposes and tests hypotheses on registered data;
  - produces evidence records and registration entries in `docs/experiments/LOG.md`;
  - can never change a status or a decision parameter by itself.
- **Selection / allocation** (`engine/decision.py`, `engine/strategy_selector.py`,
  `portfolio/*`):
  - chooses only among strategies whose **committed** lifecycle status allows it;
  - uses only parameters frozen in code or config (versioned, fingerprinted).
  - **No online learning here**, now enforced: learning is off by default.
- **Execution** (`execution/*`, `core/order_manager.py`):
  - transmits the chosen proposal after its own pure and broker checks;
  - never re-decides the strategy;
  - refuses strategies whose status is not paper-tradable (`strategy_not_paper_eligible`).

**Research → Selection is a human-reviewed commit** (a lifecycle change, a calibration table),
triggered by a registered test's binding result. Nothing else crosses that boundary.

## 5. Evidence per strategy × context (design; not implemented; corrected after review)
- **Unit:** per-opportunity forward R net of cost. Store it with the versions of the strategy,
  the regime detector and the context length. Aggregate at read time.
- **Clustering:** by trading day, and by (symbol, bar), because all strategy evaluations of one
  decision share the same price path.
- **Model:** empirical-Bayes hierarchy μ_strategy + δ_regime (+ δ_asset_class).
  - The shrinkage weight is τ² / (τ² + SE²_clustered), not a raw-count formula. σ and τ are
    **estimated from data**.
  - Shrinkage is continuous. There is no "τ̂ > 0" pre-test, which would be model selection;
    with only 4 regimes τ is barely identified.
  - Symbol-level cells are not modelled.
  - HIGH_VOLATILITY has no selected trades (the pause), so no evidence exists there.
- **Minimum samples:** derived from power:
  n ≈ ((z_{1−α/(2K)} + z_β)·σ/δ)² × design effect,
  at a registered minimum detectable effect δ and cell count K. With σ ≈ 1–1.3R, δ = 0.2R and
  K = 40, that is **≈ 410–700 trades per cell before the design effect**. None of these are
  chosen by hand; where σ is unknown, a registered pilot estimates it first.
- **Selector use:**
  1. First a calibration score → expected forward R. A method that yields uncertainty is
     required (e.g. Bayesian monotone regression, or isotonic regression with bootstrap
     intervals).
     - It is **fitted on one registered window and evaluated on a later, disjoint one**, to
       avoid winner's curse.
     - It is degenerate for constant-score strategies (swing_structure = 70).
     - Selected-only data are range-restricted (scores ≥ 55); the calibration must use all
       opportunities (`shadow_opportunities`) after the embargo.
  2. Then rank by the calibrated lower bound of expected R net of FULL cost (commission at the
     proposed size included) per unit of risk.
  3. Each calibration is a registered test.
- **New strategies:** start from the pooled prior of peer strategies (probably ≤ 0 after costs),
  not a neutral "unknown", and stay SHADOW.
- **Degradation, demotion, retirement:** ONE pre-registered sequential procedure on forward R,
  a group-sequential test or SPRT with fixed α/β. There is no continuously monitored posterior
  threshold. Repeated demote/re-promote cycles count in K. Retiring a strategy during an open
  FWD window ends that window (a decision-path change).
- **Cannot be set without data:** σ, τ, the half-life for freshness, the score → R mapping.
  Each is fixed by a registered estimation step, never by choice.

## 6. Strategy lifecycle (implemented: statuses + gates; promotion rules: design)
`RESEARCH → SHADOW → FORWARD_VALIDATED → PAPER → LIVE_ELIGIBLE`, with `RETIRED` reachable from
any state. Each arrow is a committed change after:

| Transition | Requires |
|---|---|
| RESEARCH → SHADOW | Written hypothesis (mechanism, universe, horizon); passes TRAIN checks; registered in LOG.md. |
| SHADOW → FORWARD_VALIDATED | A pre-registered forward test (FWD-style, own α/K, binding cutoff) returns KEEP on post-registration data. |
| FORWARD_VALIDATED → PAPER | Calibration of score → R exists (registered); human review; paper plumbing proven. |
| PAPER → LIVE_ELIGIBLE | Paper-to-live gate: realized-vs-shadow reconciliation, a forward test on post-registration data (the v1 HOLDOUT is single-use for the WHOLE family, on a biased cohort, and cannot be reused per strategy), human sign-off. LIVE itself stays a separate human decision. |
| Any → RETIRED | Registered degradation test, or a human decision recorded in LOG.md. |

Exploration happens only in RESEARCH and SHADOW, without capital. Today every strategy is
SHADOW. FWD1 KEEP would validate the AGGREGATE selector, not any single strategy: per-strategy
promotion needs its own registered test. "Any → RETIRED" by human decision requires the reason
to be logged in LOG.md.

## 7. Asset-class lifecycle (design: InstrumentSpec; nothing enabled)
A class becomes tradable only when **all** items are implemented, tested and journaled.
Until then discovery, pretrade and the executor refuse it (already the case), and the kill
switch never market-flattens it (implemented now).

| Requirement | STK (today) | ETF | FX (CASH) | FUT | OPT |
|---|---|---|---|---|---|
| Qualification (`reqContractDetails`) | scanner contracts | same + `stockType` (recorded now) | `Forex()`, IDEALPRO | per expiry, `lastTradeDateOrContractMonth` | chain via `reqSecDefOptParams` |
| Multiplier in risk and value | 1 | 1 | 1 (lot semantics) | required | required (100) |
| Tick / market rules | 0.01 | 0.01 | pips | per product | per premium level |
| Minimum size / increments | 1 share | 1 share | lot rules | 1 contract | 1 contract |
| Currency / FX conversion to base | USD only | USD only | required | required | required |
| Session / calendar per contract | SPY liquidHours | same | 24×5 | Globex per product | per underlying |
| Expiry / roll | — | — | — | roll policy + continuous series | expiry handling |
| Strike / right selection | — | — | — | — | chain policy |
| Liquidity measure | spread + rank | + AUM / NAV premium | spread in pips | volume / open interest | open interest, spread % of premium |
| Market-data entitlements | live required | same | required | required | required |
| Costs / slippage model | IBKR fixed per share | + ETF specifics | bps of notional | per contract + exchange | per contract |
| Sizing | 1 % risk / 10 % notional | + leverage factor | notional + leverage | multiplier-aware | premium / delta-aware |
| Margin / whatIf / leverage | cash model | leveraged ETFs | margin required | margin required | margin; short options excluded |
| Assignment / exercise | — | — | — | physical delivery avoidance | exercise / assignment handling |
| Exposure measure | notional | beta / leverage-adjusted | notional in base | notional × multiplier | delta-adjusted |
| Execution semantics | DAY LMT parent, GTC children (fixed) | same | fractional, no Adaptive | per exchange | per exchange |
| Kill-switch flatten | market order | market order | manual (implemented) | manual (implemented) | manual (implemented) |

**InstrumentSpec (PR-D):** resolved once from contract details and threaded through the
allocator (risk × multiplier), RiskManager, pretrade (tick, increments), CostModel, the session
policy and the journal (`sec_type`, currency, multiplier columns). Every consumer refuses a spec
with `supported=False` and lists what is missing.

## 8. Capital allocation (design; not implemented)
```
available capital (NetLiquidation, cash, buying power, pending orders)
 → feasible set  (spec supported; whole units under caps; FULL cost incl. commission in R
                  ≤ registered bound; PDT and T+1 settlement; size ≤ registered share of ADV)
 → comparable set (only lifecycle ≥ PAPER with a registered calibration)
 → cycle-wide ranking by the calibrated lower bound of expected R net of cost per unit of risk
 → greedy allocation under constraints: per-trade risk, portfolio heat (sum of open risk),
   GAP budget (notional × registered gap assumption, beta/leverage-adjusted), gross, cash reserve,
   sector, correlated cluster, issuer/underlying map, max concurrent positions,
   earnings-date exclusion
 → RiskManager / PortfolioBrain / CrossExposureGuard veto (unchanged, always last)
```
Current limit composition (portfolio/risk review, round 4):
- **Per-trade risk:** the 1 % budget rarely binds, because the 10 % position cap binds first.
- **Gaps:** protective stops only trigger in regular hours (`outsideRth=False`), so gaps fill
  at the open. At 80 % gross, a −10 % market gap costs about −9.6 % of equity, just under the
  10 % daily limit. One −30 % earnings gap on a 10 % position is −3 % of equity.
- **Daily loss vs drawdown:** 10 % daily loss against 15 % drawdown lets one day use two-thirds
  of the drawdown budget.
- **Recommendation (human decision, before registration, because it changes the config hash):**
  daily loss 2–3 %, drawdown about 10 %, plus the gap budget above when PR-D lands.

Other missing pieces:
- margin and buying power;
- an issuer or underlying map (SPY/VOO, GOOG/GOOGL; today only the 60-hour correlation
  catches them);
- a minimum-capital statement per instrument class;
- a check that every position has a live stop (GTC orders can be cancelled by corporate
  actions);
- a reconciliation of child quantity after a partial fill of a DAY parent.

## 9. FWD impact of this round
The decision-code fingerprint covers:
- `engine/`, `execution/`, `market/`, `portfolio/`, `risk/`, `strategies/`, `core/`;
- `config/config.py`, `backtest/costs.py`, `research/shadow_journal.py`, `run_shadow_only.py`.

The protocol fingerprint covers the evaluator, the scorer, the gates, `research/protocol.py`
and the cost model. Every change below touches one of them, which is intended: they land
**before** registration.

The golden digest (`tests/test_decision_golden.py`) was recorded AFTER the 1 % default and
tick-rounded sizing, so it certifies only the later recording-only changes. It covers the
replay path (zero costs, no sectors), not the shadow-only path, admission with sectors or
`top_*`.

| Change | Changes decisions? | Why before FWD |
|---|---|---|
| Bracket children GTC | **Yes, indirectly:** GTC exits count as pending exposure across days, tightening capacity (≈ 3 positions) | Safety bug (exits expired at the close). |
| Kill switch: non-STK skip; decides from the every-client view BEFORE cancelling; keeps the protection of every position it will not flatten; never cancels or duplicates its own pending liquidation | No | Safety. |
| Hard risk default 1 % (+ float tolerance) | Not certified by the digest; with the default allocator risk is ≤ 1 % already | Hard layer enforces what the allocator assumes. |
| Size on tick-rounded prices | Slightly (quantity) | Avoids decisions the executor would refuse. |
| Learning off by default | Paper only (shadow-only was already off) | Unvalidated evidence must not steer paper. |
| Lifecycle statuses + executor gate | No for shadow (digest); blocks autonomous paper | Frozen definition of what is evaluated. |
| Opportunity records + stock_type | No (digest); the recorder cannot break a decision | Evidence must be recorded from the first window day. |
| Instrument-type universe (COMMON/ADR/REIT) | **Yes:** ETFs and unknown types are no longer analysed | The FWD universe must be fixed before registration. |
| Daily-loss baseline rolls per day | Yes, for long-running processes (tighter) | Risk bug. |
| Registration integrity, blinding, restart guard | No | Protects the window. |

## 10. Implemented in PR #3 (this round)
See the PR body and `docs/OVERNIGHT_PROGRESS.md`: GTC protective children, kill-switch non-STK
guard, 1 % hard risk default, tick-rounded sizing, learning off by default, strategy lifecycle
+ executor gate, Opportunity records (+ `shadow_opportunities`, `stock_type`), FWD window
restart guard, FWD2 counterfactual pinned, exploratory-record embargo, pinned deployment,
golden decision digest.

## 11. Roadmap (separate PRs, each from the current base, none stacked)
1. **PR-A (this, #3):** forward-evidence infrastructure + the pre-FWD items above. Then register
   FWD-v1 from a pinned worktree.
2. **PR-B, evidence store (research only):** per-opportunity R, versioned regime labels with
   the same context length, hierarchical pooling, power calculator. Opportunity journaling in
   the replay. It runs offline on TRAIN only and changes no decision. Adding journal columns or
   versioning `market/regime.py` changes the fingerprint: merge to `feature/paper-alpha`, never
   deploy to the pinned run.
3. **PR-C, InstrumentSpec for STK (then ETF):** multiplier/tick/increment/currency plumbing,
   `sec_type` in the journal, `stockType` in the funnel (REPLAY_PARITY N13). **Changes the
   fingerprint and the universe path**: a new FWD version for anything evaluated after it.
4. **PR-D, capital feasibility:** feasible-set filter (full cost in R, whole units, PDT),
   portfolio heat, max concurrent positions, issuer map. It also covers gap budget, earnings exclusion
   and ADV participation. Registered as a decision-path change, so a NEW FWD version for
   anything evaluated after it.
5. **PR-E, calibrated comparison:** score → R calibration per strategy (after FWD1/FWD2 bind),
   cycle-wide ranking and greedy allocation. Registered as a new forward test before any
   capital.
6. **PR-F, lifecycle promotion tooling:** sequential degradation tests, promotion checklists,
   paper-to-live evidence reconciliation.
7. **PR-G+, other asset classes:** one class per PR (FX → FUT → OPT), each completing its full
   §7 column with tests. Paper only after its own shadow period.

**Fingerprint rule for every PR:** anything touching the decision path (the §9 list) or the
protocol files changes a fingerprint. Such PRs may merge to `feature/paper-alpha` during a window, but the
pinned run keeps its checkout until the window binds (at the latest 2027-03-31) or is
explicitly ended. That includes safety hotfixes: if a safety fix is urgent, end the window
(recorded and reported) rather than patching the pinned run silently.
