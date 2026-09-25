# Experiment log (protocol v1)

Protocol: `research/protocol.py` — TRAIN < 2023-09-01 ≤ VALIDATION < 2025-03-01 ≤ HOLDOUT.
Development profiles: `swing_5y` (4h bars, from 2021-09) and `long_10y` (daily, from 2016-09),
38 hand-picked current US names (`backtest/universes.py`, survivorship-biased), IBKR TRADES bars.
Engine v2 costs (IBKR fixed commission, 1.5 bps half-spread, 2 bps slippage, sell fee, borrow),
integer shares, USD 100k initial equity, pipeline caps as live (1 % risk, 10 % position, 80 % gross,
corr ≤ 0.85, long-only unless stated). Runner: `run_experiments.py`.

## Pre-registration (written before any variant was run)

Baseline H0 = `live_default` (the selector exactly as the live system would run it today).

| id | hypothesis | single change vs H0 |
|---|---|---|
| H1 | MIXED regime carries no directional information; trading it adds noise | trade only in TRENDING/RANGE |
| H2 | Low-conviction signals lose after costs | `min_score` 55 → 70 |
| H3 | A plain trend system works only when the regime is trending | trend_following + momentum_gap + breakout, TRENDING only |
| H4 | Mean reversion works only in ranges | mean_reversion + range + smc_liquidity, RANGE only |
| H5 | (research reference) shorts help | `allow_short=True` — not executable today |
| H6 | Volatility down-sizing is neutral | disable volatility multiplier |

Reference only (not a candidate): equal-weight buy-and-hold of the same cohort (shows the
survivorship tailwind the cohort enjoys).

**Decision rule (fixed in advance).** A variant is KEEP if, on VALIDATION, in BOTH development
profiles: Sharpe ≥ H0 Sharpe + 0.20, profit factor ≥ 1.05, net return > 0, ≥ 30 trades, and net
return > 0 under the STRESSED cost model; and its TRAIN Sharpe is not below H0's TRAIN Sharpe by
more than 0.20 (no regime-luck-only winners). Otherwise REJECT (or INCONCLUSIVE if trades < 30).
At most ONE frozen candidate goes to the HOLDOUT, evaluated once on both development profiles
and on `intraday_1y` (entirely holdout). No re-tuning after that.

## Round 1 results (TRAIN / VALIDATION, baseline costs; Sharpe = daily, annualized)

Reference equal-weight buy-and-hold of the cohort: 4h TRAIN +13.9 %, VALIDATION +63.7 %;
1d TRAIN +303.6 %, VALIDATION +63.7 % (survivorship tailwind: current winners picked in hindsight).

| variant | 4h TRAIN ret / Sharpe / PF | 4h VAL ret / Sharpe / PF | 1d TRAIN ret / Sharpe / PF | 1d VAL ret / Sharpe / PF | decision |
|---|---|---|---|---|---|
| H0 live_default | −14.5 % / −0.43 / 0.89 | +1.6 % / 0.15 / 1.02 | +15.7 % / 0.23 / 1.05 | −0.9 % / −0.00 / 0.98 | baseline |
| H1 no MIXED | −13.7 % / −0.44 / 0.90 | −4.0 % / −0.18 / 0.96 | +52.7 % / 0.62 / 1.17 | −5.7 % / −0.34 / 0.92 | REJECT (val worse in both) |
| H2 score ≥ 70 | −14.6 % / −0.43 / 0.90 | +7.5 % / 0.45 / 1.07 | +24.9 % / 0.32 / 1.08 | −9.0 % / −0.52 / 0.87 | REJECT (1d val) |
| H3 trend core, TRENDING | +12.0 % / 0.47 / 1.09 | +10.5 % / 0.65 / 1.09 | +26.8 % / 0.38 / 1.09 | −6.7 % / −0.42 / 0.90 | REJECT (1d val) |
| H4 reversion core, RANGE | +0.6 % / 0.11 / 1.03 | +1.1 % / 0.22 / 1.05 | −3.5 % / −0.29 / 0.83 | −2.6 % / −0.92 / 0.68 | REJECT |
| H5 with shorts | −14.7 % / −0.56 / 0.90 | −18.6 % / −1.55 / 0.83 | −33.1 % / −0.64 / 0.87 | −10.0 % / −0.92 / 0.87 | REJECT |
| H6 no vol multiplier | −17.6 % / −0.52 / 0.87 | +6.7 % / 0.43 / 1.07 | +25.1 % / 0.32 / 1.08 | +2.9 % / 0.23 / 1.05 | REJECT (stressed costs VAL: 4h −3.5 %, 1d −1.0 %) |

Trades per cell 130–1400 (H4 1d VAL: 41). Conclusion: **no variant has a robust edge; the live
selector itself is ~zero expectancy after costs** (PF 0.89–1.05) while the cohort's buy-and-hold
gained 64 % in VALIDATION. Differences of ±5 % between variants are within noise. No candidate
frozen; holdout unused.

## Round 2 pre-registration (written before running; 3 more variants → 9 tried on VALIDATION)

Motivation from literature, not from the validation numbers: long-horizon trend filters and
asymmetric exits are among the most persistent documented equity effects; current brackets
(stop 1.5 ATR / target 2.5 ATR) cap winners.

| id | hypothesis | single change vs H0 |
|---|---|---|
| H7 | Longs work only above the symbol's own long-term trend | long entries require close > SMA(200) of the symbol |
| H8 | Longs work only when the market is in an uptrend | long entries require SPY close > SPY SMA(200) on bars completed before the decision |
| H9 | Letting winners run beats capped targets | bracket recomputed at signal: stop 2 × ATR(14), target 6 × ATR(14) |

Same decision rule as round 1.

## Round 2 results

| variant | 4h TRAIN ret / Sharpe / PF | 4h VAL ret / Sharpe / PF | 1d TRAIN ret / Sharpe / PF | 1d VAL ret / Sharpe / PF | decision |
|---|---|---|---|---|---|
| H0 live_default | −14.5 % / −0.43 / 0.89 | +1.6 % / 0.15 / 1.02 | +15.7 % / 0.23 / 1.05 | −0.9 % / −0.00 / 0.98 | baseline |
| H7 symbol SMA200 | −5.7 % / −0.15 / 0.95 | −1.5 % / −0.02 / 0.99 | +13.4 % / 0.22 / 1.06 | +7.0 % / 0.46 / 1.11 | REJECT (4h val) |
| H8 SPY SMA200 | −13.1 % / −0.64 / 0.83 | +11.7 % / 0.73 / 1.12 | +17.1 % / 0.27 / 1.08 | −1.9 % / −0.06 / 0.96 | REJECT (1d val) |
| H9 ATR 2/6 bracket | −5.6 % / −0.12 / 0.94 | +2.4 % / 0.18 / 1.03 | +27.3 % / 0.35 / 1.16 | −4.3 % / −0.23 / 0.91 | REJECT |

Pattern across 9 variants: each "improvement" helps one profile and hurts the other — the
signature of noise, not edge. Continuing to mine the same VALIDATION set would mostly produce
false discoveries, so the variant loop stops here for protocol v1 (see null-model diagnostic).

## Null-model diagnostic (DEVELOPMENT = TRAIN+VALIDATION, not a selection step)

`run_null_benchmark.py`: random long entries (p = 0.1 per bar, same 1.5/2.5 ATR bracket, same
sizing, caps, regime pause and baseline costs), 6 seeds, versus `live_default`.

| profile | live_default ret / Sharpe / PF | null mean ret (sd) / mean PF | nulls ≥ live |
|---|---|---|---|
| 4h (2021-09 → 2025-03) | +4.9 % / 0.17 / 1.02 | −9.6 % (7.9) / 0.96 | 0 / 6 |
| 1d (2016-09 → 2025-03) | +23.4 % / 0.26 / 1.06 | +35.9 % (14.0) / 1.09 | 4 / 6 |

Reading: on 4h the selector is ~1.8 sd above random (weak, 6 seeds); on daily it is WORSE than
random long entries, which profit from the cohort's hindsight-selected bull run. Neither is a
robust edge. The long-only daily "returns" are largely beta + survivorship, not skill.

## Status after protocol v1
- Candidates frozen: none. HOLDOUT (≥ 2025-03-01, incl. all of intraday_1y): **unused**.
- Variants evaluated on VALIDATION: 9 (+ baseline). Any future candidate must be reported with
  that count.
- Main blocker to honest equity research is data, not strategies: the cache is a 38-name,
  hindsight-selected cohort with no delisted names and no point-in-time universe. Long-biased
  results on it are not interpretable as edge. Next research cycle should start with a
  point-in-time universe (or scanner-snapshot history recorded going forward by shadow mode).

## Round 3 pre-registration — new families, cohort-neutral event study (written before any run)

Why a different instrument: rounds 1–2 measured absolute P&L of long-only trading on a cohort
whose buy-and-hold gained 64 % in VALIDATION, so any long signal "earned" beta + survivorship.
`research/event_study.py` measures each signal's forward return from the NEXT open minus the
same-window equal-weight return of the other 37 names, minus round-trip costs (baseline: 2×3.5 bps
marketable + 0.3 bps fee + 2 bps commission allowance ≈ 9.3 bps). This removes the common drift;
it does NOT remove survivorship inside the cross-section (winners chosen with hindsight may look
like "momentum"), which is disclosed per family.

Data: `long_10y` (daily) is primary; `swing_5y` (4h, horizons in bars) is the robustness profile.
Segments: protocol v1 calendar (TRAIN < 2023-09-01 ≤ VALIDATION < 2025-03-01); a window counts in a
segment only if entry AND exit fall inside it. HOLDOUT untouched.

| id | family | economic rationale | signal (sees bars ≤ t only) | side | horizon |
|---|---|---|---|---|---|
| F1 | Cross-sectional momentum 12-1 | under-reaction / slow diffusion of information (Jegadeesh–Titman) | first bar of each month: return from t−252 to t−21 in the top quintile of the cohort | long | 21 bars |
| F2 | Short-term reversal | liquidity provision: short-horizon overshoots mean-revert | 5-bar return in the bottom quintile of the cohort (every bar) | long | 5 bars |
| F3 | Volatility contraction breakout | volatility clusters; a breakout from compression with volume signals information arrival | ATR(5)/ATR(50) in its lowest 10 % of the last 250 bars within the last 5 bars, AND close > prior 20-bar high, AND volume > 1.5 × 20-bar average | long | 10 bars |
| F4 | Gap-down reversal | overnight overreaction reverses over days | open ≤ −3 % vs prior close AND close < prior close | long | 5 bars |
| F5 | Market-residual reversal | idiosyncratic (not market) overshoots revert | 5-bar return minus SPY 5-bar return in the bottom decile (SPY/QQQ/IWM/DIA excluded as events) | long | 5 bars |
| F6 | Live selector signal (diagnostic) | does the current selector carry cross-sectional information at all? | `StrategySelector` (all strategies, threshold 55, no learning) selects LONG | long | 5 bars |

**Multiple testing.** Tests on VALIDATION so far: 9 (rounds 1–2) + 6 here = K = 15. One-sided
Bonferroni at α = 0.05 → VALIDATION requires Newey–West t ≥ 2.71.

**KEEP rule (fixed in advance)** — all of:
1. daily TRAIN: mean net excess > 0 and NW t ≥ 2.0;
2. daily VALIDATION: mean net excess > 0 and NW t ≥ 2.71, ≥ 100 events (F1: ≥ 12 rebalance dates);
3. 4h VALIDATION: same sign (F1: instead, both halves of daily TRAIN positive);
4. survives destruction tests: cost × 3, entry delay 2 bars, horizon × 0.5 and × 1.5, removing the
   best 5 % of events, removing the best symbol, both halves of VALIDATION positive.
Otherwise REJECT; INCONCLUSIVE if steps 1–2 pass on fewer events than required. A KEEP here is
"worth a pipeline implementation and forward shadow evidence", not an edge claim: the cohort is
still hindsight-selected and the HOLDOUT stays unused until a frozen implementation exists.

## Round 3 results — CORRECTED (after the quant-methodology review)

The first version of this table (commit 5b6d511) had two methodological bugs found by the
independent review: (1) the Newey–West lag was set in BARS (horizon − 1) but applied to a series of
event DATES, which for monthly F1 (≈17 dates, lag 20) collapsed the variance — the reported
"t = 4.64 / 8.96" were artefacts; (2) F5 subtracted the same SPY return from every symbol, which
cannot change the ranking, so it silently re-ran F2. Fixed in fd395ff: lag = median overlap of
event windows in date units, no t below 30 dates, per-date means reported, beta-adjusted F5,
holdout bars removed from memory. Decisions did not change. The re-run F5 is the pre-registered
hypothesis correctly implemented, not a new test (K stays 15).

Net excess = per-date mean after ≈ 9.3 bps round trip (per-event mean in brackets).

| family | daily TRAIN (events / net bps / NW t) | daily VAL | 4h VAL | decision |
|---|---|---|---|---|
| F1 XS momentum 12-1 | 553 / −12.0 [−9.0] / −0.21 | 136 / +523.7 / n/a (17 dates < 30) | 4h is a different (~6-month) signal | REJECT (TRAIN fails; VAL not testable) |
| F2 short-term reversal | 13 634 / −8.4 / −1.04 | 2 960 / +2.9 / 0.16 | 7 240 / −0.4 / −0.05 | REJECT |
| F3 vol-compression breakout | 201 / −10.7 [+22.6] / −0.21 | 55 / +220.8 / 1.57 | 174 / −31.0 / −0.71 | REJECT |
| F4 gap-down reversal | 1 010 / +23.1 / 0.61 | 191 / +224.0 / 2.63 | 198 / +13.3 / 0.31 | REJECT (VAL t < 2.71, TRAIN fails) |
| F5 beta-residual reversal | 6 720 / −8.2 / −0.63 | 1 480 / +9.7 / 0.33 | 3 620 / +7.0 / 0.63 | REJECT |
| F6 selector-logic LONG | 29 546 / −8.0 / −1.24 | 6 841 / **−37.0** / **−3.64** | 17 632 / −14.8 / −3.42 | REJECT (negative) |

Reading (worded per the review):
- **F1**: nothing in seven TRAIN years; the large VALIDATION mean comes from 17 monthly dates in
  2023-09 → 2025-03, when the hindsight-picked winners led. Not testable here; re-test only on a
  point-in-time universe (FWD3).
- **F6**: the selector's logic, run on DAILY and 4h bars (the live system runs it on 1-hour bars),
  picks LONG setups that underperform the rest of the cohort after costs in VALIDATION on both
  profiles; two-sided Bonferroni (K = 15 → 30 tails) needs |t| ≥ 2.94 and both clear it. TRAIN is
  ≈ −1 bps gross (t −1.24): no information there, not anti-predictive. Supported conclusion: the
  current selector logic shows no positive cross-sectional information and should not be trusted
  with capital. It says nothing definitive about the live 1-hour configuration (FWD1 will).
- Near misses (F3, F4 in VALIDATION) fail TRAIN and/or the other profile.
- "Net excess" is not a tradeable long/short return: the benchmark leg carries no cost, and the
  flat 2 bps commission allowance understates IBKR minimums for small orders.

## Diagnostic: baseline under replay v3 (not a test, no selection)

Replay v3 (commit 955edc2+) mirrors the executor: LIMIT at the signal close with DAY validity,
strict trade-through, 3 entries/day, score-ordered cycles, working orders in correlation. The
unchanged `live_default` now shows positive absolute results (daily TRAIN +48.2 %, PF 1.18;
daily VAL +10.7 %; 4h VAL +19.4 %, Sharpe 1.16) — while the cohort's buy-and-hold made +304 % /
+64 % in the same windows.

Null model under the SAME v3 mechanics (`run_null_benchmark.py --segment development`, 6 seeds):

| profile | live_default | null mean (sd) | nulls ≥ live |
|---|---|---|---|
| 4h DEVELOPMENT | +6.97 % / PF 1.03 | +6.96 % (9.49) / PF 1.03 | 3 / 6 |
| 1d DEVELOPMENT | +62.3 % / PF 1.17 | +47.6 % (29.6) / PF 1.14 | 2 / 6 |

Reading: the improvement versus replay v1 comes from execution mechanics (buying pullbacks to
the signal close with limit orders, in a cohort that rose strongly), not from the selector —
random entries with the same brackets earn the same. Consistent with F6. No claim of edge.

## Stop condition reached on this dataset
Tests on the protocol-v1 VALIDATION set: **15** (H1–H9 pipeline variants + F1–F6), plus
diagnostics (baseline, null models) not used for selection. Further searching on the same 38-name,
hindsight-selected sample would mostly manufacture false discoveries. The HOLDOUT (≥ 2025-03-01)
remains unused. Research continues on NEW data only.

## Forward-only hypotheses (pre-registered now; evaluated ONLY on shadow data recorded after 2026-09-24)
Scored with `run_score_shadow.py` on journaled decisions (point-in-time universe, 1-hour bars,
executor-style LIMIT/DAY entries). No parameter may change between now and evaluation.
| id | hypothesis | measurement | decision rule |
|---|---|---|---|
| FWD1 | Selected LONG setups are not better than the cohort (replication of F6 on 1-hour bars) | GROSS 5-bar forward return of SELECTED decisions minus the same-cycle mean (`selected_vs_cycle_fwd5`), t over per-cycle means | after ≥ 300 non-duplicate selected decisions: a mean above the ≈ 9 bps round-trip cost with t ≥ 2 would contradict F6 |
| FWD2 | NO_TRADE passes on setups as good as the ones it takes | `counterfactual_vs_cycle_fwd5` vs `selected_vs_cycle_fwd5` (same measure) | difference of per-cycle means with t; ≥ 300 each |
| FWD3 | Cross-sectional 12-1 momentum works on the point-in-time scanner universe | F1 signal on `JournalUniverse` + `reports/pit_cache` bars | needs ≥ 12 monthly rebalances of journal data; same KEEP rule as round 3 |

**Exact evaluation (added before any v2 forward data exists, hypotheses unchanged; revised
once after the quant review of the same day, still before any v2 data):**
`docs/FWD_PROTOCOL.md` (FWD-v1) fixes the evidence window (v2 decisions only), population,
leave-one-out benchmark, day-clustered inference with Hansen–Hodrick lag-1 weights and Student-t
critical values (K = 3: about 2.50 at 40 days, 2.39 in the limit), binding cutoff, sample
minimums, KEEP/REJECT/INCONCLUSIVE rules, deadlines and invalidation. It supersedes the
shorthand rules in this table where they differ (e.g. "t ≥ 2" → 2.40 after the K = 3 correction;
"t over per-cycle means" → per-day NW, which is more conservative).
