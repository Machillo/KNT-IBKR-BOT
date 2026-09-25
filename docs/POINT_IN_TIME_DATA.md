# Point-in-time data: what exists, what is missing, how it plugs in

## Why
Every historical result so far comes from `reports/history_cache`. That cache is flawed in
three ways:
- it holds 38 US names chosen today, with hindsight;
- it has no delisted securities;
- it has no record of what IBKR's scanners returned in the past.

Any long-biased result on it mixes skill with survivorship (see `docs/experiments/LOG.md`).

## What KNT records by itself (no external data, accumulating from the first shadow cycle)
Every shadow cycle goes through `engine/shadow.py` → `research/shadow_journal.py` (SQLite). The
shadow-only process writes to `state/shadow_only/strategy_performance.db`. The journal is v2
(`DECISION_VERSION = selector_v1+decision_pipeline_v2`).

| Table | Content |
|---|---|
| `discovery_cycles` | Cycle id and UTC time; universe; scanners; rows per scanner requested; rows returned and failures per scanner; quote budget; market-data type; session state; eligible count; candidates attempted, errors and cap; run mode; learning mode; git commit; decision version; completion time. |
| `discovery_funnel` | Every contract any scanner returned: symbol, conId, exchange, currency, best rank, every `(scanner, rank)`, status (`subtype_rejected`, `not_quoted_budget`, `quote_error`, `ranked_eligible`, `ranked_rejected`), reason, price, spread, volume, liquidity score. |
| `shadow_decisions` | Every outcome, including NO_TRADE and CANDIDATE_ERROR: bar time, bar count, first bar; regime and action; selected and best sub-threshold signal; ATR, ADX, volatility stress, threshold, selector bonus. Also: sizing (quantity, notional, risk, volatility multiplier), sector, correlation, fresh reference quote and its data type, session state, run and learning mode, duplicate/conflict flags. |
| `shadow_outcomes` | Scorer v2 results: executable flag, filled, exit reason, net return, MAE/MFE, bars held, 1/5/20-bar forward returns, provider. |
| `research_quality` | Data-quality gate results and the session verdict (`run_shadow_report.py --persist`). |

This record is survivorship-free by construction: it is exactly what KNT knew at each moment.
`research/universe_provider.JournalUniverse(top_n=SHADOW_MAX_CANDIDATES)` replays it as a
point-in-time universe. Stale or empty snapshots give an empty universe (fail closed).
`run_fetch_journal_bars.py` fetches bars for every journaled (symbol, conId), read-only, and
MERGES them with what is already stored, so older history survives IBKR's rolling window.
Names that stop trading are kept too, as long as they are fetched before IBKR drops them.

## External dataset specification v1 (not purchased; needs a human decision on cost and licence)

### Purpose
Research the PAST honestly: survivorship-free replays of the live 1-hour decision path, and
FWD3-style monthly studies with more history than the forward journal can provide.

### Coverage
- **Universe:** all US-listed common stocks (NYSE, NASDAQ, NYSE American, Arca-listed
  stocks), including securities that have since been delisted. Exclude ETFs, ADRs, preferreds,
  rights, warrants and units, or flag them so they can be excluded (KNT's funnel rejects them).
- **Depth:** at least 2015-01-01 to today for daily data, and at least 2019-01-01 to today for
  1-hour bars. More depth only helps FWD3 (monthly), where more months are needed.
- **Survivorship:** every delisted security in the period, with its full history up to the
  last trading day.

### Files, fields and conventions (CSV, UTF-8, one header row)

1. **`membership.csv`** (point-in-time investable universe):
   - Columns: `date, symbol, identifier, in_universe, delisted_on`.
   - Frequency: daily (weekly is acceptable). `date` is the exchange date. A row means the
     membership was KNOWN AT THE CLOSE of `date`.
   - `identifier` is permanent across ticker changes: FIGI (share class), CRSP PERMNO, or the
     vendor's permanent id. `delisted_on` is empty while the security is listed.
   - The universe is computed only from data available on `date`: for example, price ≥ $5 and
     20-day median dollar volume ≥ $5M, both computed through `date`.
   - Loader: `PointInTimeCsvUniverse`. Effective from D+1. It refuses to load files with
     missing columns or values, an ambiguous ticker, or a member on or after its own delisting.
2. **`bars_1h/{identifier}.csv`** and **`bars_1d/{identifier}.csv`** (prices):
   - Columns: `time, symbol, open, high, low, close, volume`.
   - `time` is the bar START with an explicit UTC offset. Bars are regular trading hours
     only; the first hourly bar is 09:30–10:00 ET, as IBKR returns it.
   - Prices are RAW (as traded, unadjusted), because order sizing and price filters need
     tradable prices. `symbol` is the ticker valid at that bar.
   - Integration: convert to the `PriceBar` JSON layout of `reports/history_cache` (one file
     per symbol and profile). The replay keys times in exchange time (`_key` converts UTC).
3. **`corporate_actions.csv`**:
   - Columns: `ex_date, identifier, type (split|cash_dividend|spinoff|symbol_change|delisting), ratio_or_amount, new_symbol, delisting_return`.
   - Signals may use split-adjusted series built from this file. Research returns must be
     total returns: dividends included, plus the delisting return on the last day (never
     silently 0).
4. **`sectors.csv`**:
   - Columns: `date, symbol, identifier, sector`.
   - Sector as known on `date` (GICS sector or an equivalent stable taxonomy), with
     historical changes kept.
   - Loader: `PointInTimeSectors`. Same lag convention. Unknown or stale gives None, which
     fails the sector cap closed, exactly as at runtime.
5. **`identifier_map.csv`** (optional but valuable):
   - Columns: `identifier, valid_from, valid_to, ibkr_con_id`.
   - Links the vendor identifier to IBKR contracts, so forward journal rows and historical
     rows can be joined.

### Acceptance checks, run BEFORE any result is looked at
- **Survivorship:** at least 15 % of identifiers present in 2015 are delisted by today (the
  US base rate is well above that). A lower share means dead names are missing.
- **Membership look-ahead:** recompute the liquidity filter from the bars for a sample of
  dates; it must match `in_universe` using only data through `date`.
- **Corporate actions:** raw close × cumulative split factor is continuous across split dates
  on a sample.
- **Delistings:** every `delisted_on` has a last bar within 5 trading days before it and a
  `delisting_return` row.
- **Timezone:** the first hourly bar of a random sample of days starts at 09:30 ET.
- **Overlap with IBKR:** for the overlap period, hourly OHLC matches IBKR bars for the same
  conId within 0.5 % on at least 99 % of bars.

### Integration points (implemented, tested with synthetic fixtures)
- **Membership:** `research/universe_provider.PointInTimeCsvUniverse`.
- **Sector:** `research/universe_provider.PointInTimeSectors`. The replay's sector-cap
  wiring is the remaining step (REPLAY_PARITY #5).
- **Bars:** the `reports/history_cache` JSON layout, loaded by `run_pipeline_backtest.load`
  and `research.shadow_scoring.CacheBarsProvider`.
- **Replay:** `PipelineBacktest(data, PipelineConfig.from_bot_config(config), universe=...)`,
  `PIPELINE_VERSION = 4`, same `DecisionPipeline` and pre-trade checks as the runtime.
- **Protocol:** define protocol v2 (a new calendar split on the NEW dataset) BEFORE looking at
  any result on it. The v1 holdout (≥ 2025-03-01) stays unused for selection.

## Legitimate offline expansion available now
None removes hindsight:
- the cache cannot be extended with names that were not chosen with hindsight;
- IBKR does not offer historical scanner results.

The honest paths are forward recording (above) and/or an external survivorship-free source
meeting the specification.
