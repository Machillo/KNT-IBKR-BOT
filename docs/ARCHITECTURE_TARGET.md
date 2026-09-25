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
| Portfolio risk | Exists (single-opportunity checks) | Gross 80 %, cash reserve net of pending orders, daily loss, drawdown, sector 30 %, correlated cluster 40 %, \|corr\| > 0.85. No total open-risk ("heat") limit, no margin, no issuer map. |
| Risk veto | Exists and correct | RiskManager sits on every approval path, and nothing overrides it. |
| Learning / performance tracking | Exists, **not valid** | Thresholds unjustified. Research windows overlap the holdout. Regime labels mismatched. **Now OFF by default everywhere.** |
| Shadow learning / evidence | Exists | Journal v2 + opportunities, scorer v3, quality gates, FWD-v1. |
| Replay | Exists (v4) | Parity documented in REPLAY_PARITY. Sector cap not enforced in replay (no point-in-time sectors). |
| Point-in-time data | Partial | Forward journal plus a spec for external data. History is survivorship-biased (38 names). |
| Execution | Exists, STK-only | **Fixed now:** bracket children were DAY. **Fixed now:** kill switch no longer market-flattens non-STK. |
| Contract abstraction | Missing | No InstrumentSpec. `multiplier`, `minTick`, market rules, margin and `whatIf` are never read. |
| Session / calendar | STK-coupled | One global US-equity session from SPY `liquidHours`; ET dates hardcoded; 1 h timeframe hardcoded. |

### Selector census (descriptive, TRAIN only, no returns looked at)
`tools/selector_census.py` (run 2026-09-25 on the 38-name cache, bars < 2023-09-01, 140-bar context,
learning off):
- The selector chooses a trade in **86 % (4h) / 76 % (daily)** of evaluations.
- NO_TRADE comes almost only from the high-volatility pause. The threshold of 55 binds in
  ~1 % of evaluations.
- **momentum_gap_v1 is selected 51 % / 57 % of the time, even in RANGE (28–34 %)**.
  momentum_gap + trend_following + swing_structure together take ~93 %. swing_structure's score
  is the constant 70.

It is effectively a trend/momentum basket behind a selector, not evidence-based multi-strategy
selection. This is not changed now: FWD1 is testing exactly this selector. Changing it would
change the object under test. Replacing it is PR-B below.

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

## 5. Evidence per strategy × context (design; not implemented)
- **Unit:** per-opportunity forward R net of cost. Store it with the versions of the strategy,
  the regime detector and the context length. Aggregate at read time.
- **Model:** empirical-Bayes hierarchy μ_strategy + δ_regime (+ δ_asset_class). Standard errors
  are clustered by trading day. The shrinkage weight is n / (n + σ²/τ²), with σ and τ
  **estimated from data**. If τ̂ ≈ 0, use global evidence only.
  - Symbol-level cells are not modelled: they would essentially never get weight.
- **Minimum samples:** derived from power, n ≈ ((z_α/K + z_β)·σ/δ)², at a registered minimum
  detectable effect δ and cell count K. They are not chosen by hand. Where σ is unknown, a
  registered pilot estimates it first.
- **Selector use:**
  1. First a calibration: score → expected forward R (isotonic regression, on forward data only).
  2. Then rank by a lower bound (e.g. the 25th percentile of the posterior) of expected R net of
     cost per unit of risk.
  3. Each calibration is a registered test.
- **New strategies:** start from the pooled prior of peer strategies (probably ≤ 0 after costs),
  not a neutral "unknown", and stay SHADOW.
- **Degradation, demotion, retirement:** a pre-registered sequential test on forward R (CUSUM
  or SPRT with fixed α/β). Demote when P(μ < 0) exceeds a registered level; retire when the
  upper bound stays below cost for a registered number of trades. Hysteresis is required.
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
| PAPER → LIVE_ELIGIBLE | Paper-to-live gate: realized-vs-shadow reconciliation, one-time HOLDOUT test of the frozen implementation, human sign-off. LIVE itself stays a separate human decision. |
| Any → RETIRED | Registered degradation test, or a human decision recorded in LOG.md. |

Exploration happens only in RESEARCH and SHADOW, without capital. Today every strategy is
SHADOW.

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
 → feasible set  (whole units under caps; full cost in R ≤ registered bound; PDT/settlement; spec supported)
 → comparable set (only lifecycle ≥ PAPER with a registered calibration)
 → cycle-wide ranking by lower-bound expected R net of cost per unit of risk
 → greedy allocation under constraints: per-trade risk, portfolio heat, gross, cash reserve,
   sector, correlated cluster, issuer/underlying map, max concurrent positions
 → RiskManager / PortfolioBrain / CrossExposureGuard veto (unchanged, always last)
```
Missing pieces to add: portfolio heat (sum of open risk), margin and buying power, an issuer or
underlying map (e.g. SPY/VOO, GOOG/GOOGL), an explicit maximum number of concurrent positions,
and a minimum-capital statement per instrument class.

## 9. FWD impact of this round
The decision-code fingerprint covers `engine/`, `execution/`, `market/`, `portfolio/`,
`risk/`, `strategies/`, `core/`, `config/config.py`, `research/shadow_journal.py` and
`run_shadow_only.py`. Every implemented change in §10 touches it, which is intended: they land
**before** registration.

| Change | Changes decisions? | Why before FWD |
|---|---|---|
| Bracket children GTC | No (execution only) | Safety bug; also parity with the scorer's persistent bracket. |
| Kill switch skips non-STK | No | Safety. |
| Hard risk default 1 % | No for the default allocator (risk ≤ 1 % already); tighter ceiling | Hard layer enforces what the allocator assumes. |
| Size on tick-rounded prices | Slightly (quantity) | Avoids decisions the executor would refuse. |
| Learning off by default | Paper only (shadow-only was already off) | Unvalidated evidence must not steer paper. |
| Lifecycle statuses + executor gate | No for shadow (golden digest); blocks autonomous paper | Frozen definition of what is evaluated. |
| Opportunity records + stock_type | No (golden digest) | Evidence must be recorded from the first window day. |
| Restart guard on the registered window | No | Protects the window. |

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
   the same context length, hierarchical pooling, power calculator. It runs offline on
   TRAIN/VALIDATION replays and changes no decision.
3. **PR-C, InstrumentSpec for STK/ETF:** multiplier/tick/increment/currency plumbing, ETF
   classification (leveraged/inverse refused or leverage-adjusted), `sec_type` in the journal.
   STK behavior identical (golden digest).
4. **PR-D, capital feasibility:** feasible-set filter (full cost in R, whole units, PDT),
   portfolio heat, max concurrent positions, issuer map. Registered as a decision-path change,
   so a NEW FWD version for anything evaluated after it.
5. **PR-E, calibrated comparison:** score → R calibration per strategy (after FWD1/FWD2 bind),
   cycle-wide ranking and greedy allocation. Registered as a new forward test before any
   capital.
6. **PR-F, lifecycle promotion tooling:** sequential degradation tests, promotion checklists,
   paper-to-live evidence reconciliation.
7. **PR-G+, other asset classes:** one class per PR (FX → FUT → OPT), each completing its full
   §7 column with tests. Paper only after its own shadow period.
