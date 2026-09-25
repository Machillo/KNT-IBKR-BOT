# Point-in-time data — what exists, what is missing, how it plugs in

## Why
Every historical result so far comes from `reports/history_cache`: 38 US names chosen today
(hindsight), no delisted securities, no record of what IBKR's scanners returned in the past. Any
long-biased result on it mixes skill with survivorship. See `docs/experiments/LOG.md`.

## What KNT now records by itself (no external data, starts accumulating immediately)
Every shadow cycle (`engine/shadow.py` → `research/shadow_journal.py`, SQLite in `state/`):

| table | content |
|---|---|
| `discovery_cycles` | cycle id, UTC time, universe name, scanners used, rows per scanner, quote budget, market-data type, session state |
| `discovery_funnel` | every contract any scanner returned: symbol, conId, exchange, currency, best rank, all `(scanner, rank)` sources, status (`subtype_rejected`, `not_quoted_budget`, `quote_error`, `ranked_eligible`, `ranked_rejected`), reason, price, spread bps, volume, liquidity score |
| `shadow_decisions` | every selector outcome incl. NO_TRADE: bar time, regime, action, selected signal, best sub-threshold signal, ATR/ADX/volatility stress, threshold, decision version, decision-bar close, duplicate flag |
| `shadow_outcomes` | scorer results (`run_score_shadow.py`): filled, exit reason, net return, MAE/MFE, holding bars, 1/5/20-bar forward returns |

This is survivorship-free by construction: it is exactly what KNT knew at each moment.
`research/universe_provider.JournalUniverse` replays it as a point-in-time universe
(stale or empty snapshots → empty universe, fail closed). Bars for journaled symbols are
fetched read-only later (`run_score_shadow.py --source ibkr`), including names that stop
trading — as long as they are fetched before IBKR drops them.

Expected accumulation (1-hour decisions, ≈ 4 scanners × 25 rows, ~12 deep candidates per
cycle): roughly 60–80 distinct candidate-bars per trading day. Meaningful per-strategy/regime
statistics need months, not days.

## What an external dataset would need (to research the past honestly)
Not purchased or downloaded — requires a human decision (cost, licence).

Must have:
1. **Delisted securities** with full price history up to delisting and a delisting date/reason.
2. **Point-in-time membership** of the investable universe (e.g. all US common stocks above a
   price/liquidity floor as of each date), not today's constituents.
3. **Stable identifiers** across ticker changes (e.g. FIGI / exchange-assigned permanent id),
   plus a map to IBKR conId where possible.
4. **Corporate actions** (splits, dividends) with an explicit adjustment convention; returns
   series that include dividends for total-return evaluation.
5. **Timestamps with timezone** and bar-completion semantics (bar start vs end).
6. Daily bars at minimum; intraday (hourly) to match the live 1-hour decision timeframe.

Biases to check before trusting it:
- survivorship (are dead names present?), look-ahead in membership (is a date's membership
  only what was knowable that day?), back-filled fundamentals/restated data, split-adjusted
  prices used as if they were traded prices (position sizing, price filters), liquidity filters
  computed with future volume.

## How it integrates (already implemented)
- Membership → `research/universe_provider.PointInTimeCsvUniverse`:
  CSV `date,symbol,identifier,in_universe[,delisted_on]`; a snapshot dated D is effective from
  D + `effective_lag_days` (default 1), delisting enforced against the query time, snapshots
  older than `max_age_days` → empty universe.
- Bars → the same `PriceBar` JSON layout as `reports/history_cache` (one file per symbol and
  profile), loaded by `run_pipeline_backtest.load` / `research.shadow_scoring.CacheBarsProvider`.
- Replay → `PipelineBacktest(data, config, universe=PointInTimeCsvUniverse(...))`: decisions only
  on symbols that were members at decision time; same `DecisionPipeline` as runtime.
- Protocol → define protocol v2 (new calendar split) BEFORE looking at any result on the new
  data; the v1 holdout (≥ 2025-03-01) must stay unused for selection.

## Legitimate offline expansion available now
None that removes hindsight: the cache cannot be extended with names that were not chosen with
hindsight, and IBKR does not offer historical scanner results. The honest path is forward
recording (above) and/or an external survivorship-free source.
