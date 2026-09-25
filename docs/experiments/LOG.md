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
