---
name: market-discovery
description: How KNT discovers its tradable universe dynamically (IBKR scanners → subtype filter → quote budget → liquidity ranking), how every step is recorded point-in-time, and how research must use discovery data without hindsight. Use when changing discovery, ranking, universe definitions, scanners, adding an asset class, or building research universes.
---

# Market discovery

KNT never trades a fixed ticker list. Runtime discovery is broker-native and recorded exactly as
it happened, so research can later replay what was knowable at each moment.

## Runtime funnel (market/)
1. **Scanners** — `UniversePlan` of `ScannerPlan`s (`market/universe.py`); today
   `US_STOCK_OPPORTUNITY_UNIVERSE`: STK / STK.US.MAJOR with MOST_ACTIVE, TOP_PERC_GAIN,
   TOP_PERC_LOSE, HOT_BY_VOLUME, `DISCOVERY_ROWS` rows each (IBKR max 50).
2. **Dedup** — `IBKRDiscoveryService.scan_many` merges by (secType, conId), keeps the best rank
   and **every** `(scanner, rank)` source. A failing scanner is skipped, not fatal.
3. **Subtype filter** — `_is_common_stock_candidate`: STK only, no symbols with spaces (rights,
   preferreds, share classes) until subtype-aware models exist → `subtype_rejected`.
4. **Quote budget** — best-ranked `UNIVERSE_QUOTE_BUDGET` get a snapshot (pacing / data lines);
   the rest → `not_quoted_budget`. Snapshot failure → `quote_error`.
5. **Liquidity ranking** — `LiquidityRanker`: price ≥ 1, two-sided quote, spread ≤ 50 bps;
   score = 100 − 2·rank − 0.5·min(spread, 50) → `ranked_eligible` / `ranked_rejected` + reason.
6. Top `SHADOW_MAX_CANDIDATES` eligible → completed-bar history → `DecisionPipeline`.

Execution-side gates are separate (session calendar, live quote, sector metadata, risk) and
live in `execution/`, `portfolio/`, `market/session.py`.

## Point-in-time record (research/shadow_journal.py)
Every cycle writes `discovery_cycles` (scanners, budgets, data type, session) and
`discovery_funnel` (every contract, sources, status, reason, price, spread, volume, score);
decisions go to `shadow_decisions`. Never update rows; add columns via migration.

## Research without hindsight
- Use `research/universe_provider.py`: `JournalUniverse` (what KNT's scanners returned;
  stale/empty snapshot → nobody tradable), `PointInTimeCsvUniverse` (external survivorship-free
  source, snapshot effective next day, delistings enforced), `StaticCohortUniverse` only for
  diagnostics — it is flagged `survivorship_biased`.
- Never build a research universe from today's scanner output or today's index constituents and
  apply it to the past. Never filter by liquidity/price using future volume or prices.
- Report which provider was used and whether it is survivorship-biased.
- Data requirements for external sources: `docs/POINT_IN_TIME_DATA.md`.

## Adding a market segment or asset class
- A new `ScannerPlan` is enough for another STK segment; discovery and journaling are generic.
- FX/futures/options need, BEFORE discovery can feed decisions: contract qualification
  (multiplier, tick size, min size, expiry/roll), a session policy, a cost model, sizing in
  contract units, and an executor that accepts the secType. Today the executor rejects anything
  but STK — keep it that way until those exist.
- Keep new fields generic (`sec_type`, `con_id`, `currency`) so the funnel records any asset.
