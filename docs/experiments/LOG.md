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
