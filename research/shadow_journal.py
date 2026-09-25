"""Forward-recorded, point-in-time research data from shadow mode.

The cached history is a hindsight-selected cohort (survivorship bias) and cannot
reproduce what IBKR scanners offered at each moment. From now on every shadow cycle
records:

* ``discovery_snapshots`` — the ranked universe the scanners actually produced
  (point-in-time, includes names that later disappear);
* ``shadow_decisions`` — every selector outcome (including NO_TRADE) with the
  signal's entry/stop/target, regime and the bar time it was computed from.

``score_decision`` evaluates a recorded decision on bars that came AFTER it, using
the same fill semantics as the backtester (next-bar-open entry, gap-aware stop,
limit target, stop wins ties). Scores are research evidence only; they never feed
live decisions directly.
"""
from __future__ import annotations

from config.config import state_path

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import json
import sqlite3

from backtest.costs import BASELINE, CostModel
from market.history import PriceBar


# Context stored with every decision so NO_TRADE can be evaluated counterfactually later.
_DECISION_EXTRA_COLUMNS = (
    ("top_strategy", "TEXT"), ("top_side", "TEXT"), ("top_score", "REAL"),
    ("top_entry", "REAL"), ("top_stop", "REAL"), ("top_target", "REAL"),
    ("atr", "REAL"), ("adx", "REAL"), ("volatility_stress", "REAL"),
    ("liquidity_score", "REAL"), ("selector_threshold", "REAL"), ("decision_version", "TEXT"),
    ("reference_close", "REAL"), ("duplicate_of", "INTEGER"),
)
DECISION_VERSION = "selector_v1+decision_pipeline_v1"


class ShadowJournal:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else state_path("strategy_performance.db")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS discovery_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cycle_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    con_id INTEGER,
                    scanner_rank INTEGER,
                    liquidity_score REAL,
                    eligible INTEGER NOT NULL,
                    reference_price REAL,
                    spread_bps REAL,
                    reason TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS shadow_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cycle_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    con_id INTEGER,
                    bar_time TEXT,
                    timeframe TEXT NOT NULL,
                    regime TEXT NOT NULL,
                    action TEXT NOT NULL,
                    strategy TEXT,
                    side TEXT,
                    score REAL,
                    entry REAL,
                    stop REAL,
                    target REAL,
                    reason TEXT
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_shadow_decisions_symbol ON shadow_decisions(symbol, id)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS discovery_cycles (
                    cycle_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    universe TEXT,
                    scanners TEXT,
                    rows_per_scanner INTEGER,
                    quote_budget INTEGER,
                    market_data_type INTEGER,
                    session TEXT,
                    market_open INTEGER
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS discovery_funnel (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cycle_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    con_id INTEGER,
                    sec_type TEXT,
                    exchange TEXT,
                    currency TEXT,
                    best_rank INTEGER,
                    sources TEXT,
                    status TEXT NOT NULL,
                    reason TEXT,
                    reference_price REAL,
                    spread_bps REAL,
                    volume REAL,
                    liquidity_score REAL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_discovery_funnel_cycle ON discovery_funnel(cycle_id)")
            existing = {row[1] for row in conn.execute("PRAGMA table_info(shadow_decisions)")}
            for name, ddl in _DECISION_EXTRA_COLUMNS:
                if name not in existing:
                    conn.execute("ALTER TABLE shadow_decisions ADD COLUMN " + name + " " + ddl)

    @staticmethod
    def new_cycle_id() -> str:
        return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")

    def record_discovery(self, cycle_id: str, ranked) -> int:
        now = datetime.now(timezone.utc).isoformat()
        rows = [
            (cycle_id, now, str(c.symbol), int(getattr(c.contract, "conId", 0) or 0), int(c.scanner_rank),
             float(c.score), int(bool(c.eligible)), c.reference_price, c.spread_bps, str(c.reason))
            for c in ranked
        ]
        with sqlite3.connect(self.path) as conn:
            conn.executemany(
                """INSERT INTO discovery_snapshots (cycle_id, created_at, symbol, con_id, scanner_rank,
                   liquidity_score, eligible, reference_price, spread_bps, reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
        return len(rows)

    def record_cycle(self, cycle_id: str, *, universe: str | None, scanners: list[str],
                     rows_per_scanner: int | None, quote_budget: int | None,
                     market_data_type: int | None, session: str | None, market_open: bool | None) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                """INSERT OR IGNORE INTO discovery_cycles (cycle_id, created_at, universe, scanners,
                   rows_per_scanner, quote_budget, market_data_type, session, market_open)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (cycle_id, datetime.now(timezone.utc).isoformat(), universe, json.dumps(list(scanners)),
                 rows_per_scanner, quote_budget, market_data_type, session,
                 None if market_open is None else int(bool(market_open))),
            )

    def record_funnel(self, cycle_id: str, funnel) -> int:
        now = datetime.now(timezone.utc).isoformat()
        rows = [
            (cycle_id, now, r.symbol, r.con_id, r.sec_type, r.exchange, r.currency, r.best_rank,
             json.dumps([list(x) for x in r.sources]), r.status, r.reason, r.reference_price,
             r.spread_bps, r.volume, r.liquidity_score)
            for r in funnel
        ]
        with sqlite3.connect(self.path) as conn:
            conn.executemany(
                """INSERT INTO discovery_funnel (cycle_id, created_at, symbol, con_id, sec_type, exchange,
                   currency, best_rank, sources, status, reason, reference_price, spread_bps, volume,
                   liquidity_score) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
        return len(rows)

    def record_decision(self, cycle_id: str, *, symbol: str, con_id: int, bar_time, timeframe: str,
                        regime: str, action: str, strategy: str | None, side: str | None,
                        score: float | None, entry: float | None, stop: float | None,
                        target: float | None, reason: str | None, top: dict | None = None,
                        context: dict | None = None, created_at: datetime | None = None) -> int:
        """Record one decision (append-only; never updated).

        ``top`` = best directional evaluation even when below the threshold (keys strategy,
        side, score, entry, stop, target) so NO_TRADE can be scored counterfactually;
        ``context`` = atr, adx, volatility_stress, liquidity_score, selector_threshold.
        """
        top = top or {}
        context = context or {}
        bar_text = None if bar_time is None else str(bar_time)
        with sqlite3.connect(self.path) as conn:
            # Shadow cycles run more often than bars complete: the first decision on a
            # completed bar is canonical, later ones are kept for audit but flagged.
            first = None if bar_text is None else conn.execute(
                """SELECT id FROM shadow_decisions WHERE symbol=? AND bar_time=? AND timeframe=?
                   AND decision_version=? AND duplicate_of IS NULL ORDER BY id LIMIT 1""",
                (symbol, bar_text, timeframe, DECISION_VERSION),
            ).fetchone()
            cur = conn.execute(
                """INSERT INTO shadow_decisions (cycle_id, created_at, symbol, con_id, bar_time, timeframe,
                   regime, action, strategy, side, score, entry, stop, target, reason,
                   top_strategy, top_side, top_score, top_entry, top_stop, top_target,
                   atr, adx, volatility_stress, liquidity_score, selector_threshold, decision_version,
                   reference_close, duplicate_of)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (cycle_id, (created_at or datetime.now(timezone.utc)).isoformat(), symbol, int(con_id or 0),
                 bar_text, timeframe, regime, action, strategy, side,
                 score, entry, stop, target, reason,
                 top.get("strategy"), top.get("side"), top.get("score"), top.get("entry"),
                 top.get("stop"), top.get("target"),
                 context.get("atr"), context.get("adx"), context.get("volatility_stress"),
                 context.get("liquidity_score"), context.get("selector_threshold"), DECISION_VERSION,
                 context.get("reference_close"), None if first is None else int(first[0])),
            )
            return int(cur.lastrowid)


@dataclass(frozen=True)
class DecisionOutcome:
    filled: bool
    exit_reason: str
    entry: float | None
    exit: float | None
    return_pct: float | None
    bars_held: int
    mae_pct: float | None = None  # worst adverse excursion from the entry fill, % (<= 0)
    mfe_pct: float | None = None  # best favourable excursion from the entry fill, % (>= 0)
    resolved: bool = True         # False when the bracket is still open at the end of the data


def score_decision(side: str, stop: float, target: float, bars_after: list[PriceBar],
                   costs: CostModel = BASELINE, max_bars: int = 60,
                   limit: float | None = None) -> DecisionOutcome:
    """Outcome of a LONG/SHORT bracket evaluated on ``bars_after`` (bars after the decision).

    Entry:
    * ``limit is None`` — marketable at ``bars_after[0].open`` (legacy research mode);
    * ``limit`` given — the executor's model: LIMIT, DAY validity (bars of the first
      bar's exchange date). An open through the limit fills at the open (capped at the
      limit); otherwise it fills only if price trades strictly through the limit.
      Unfilled by the end of that date -> ``limit_not_filled`` (resolved).
    Exits: gap-aware stop-market, limit target, stop wins ties. After an INTRABAR limit
    fill the target is not allowed on the fill bar (the high may have come first).
    MAE/MFE are measured from the entry fill and clipped at the exit price on the exit bar.
    Net return per share after spread/slippage/fees (commission excluded: size-dependent).
    """
    side = side.upper()
    if side not in {"LONG", "SHORT"} or not bars_after:
        return DecisionOutcome(False, "no_data", None, None, None, 0, resolved=bool(bars_after))
    long = side == "LONG"
    window = bars_after[:max_bars]

    fill_index, entry, at_open = None, None, False
    if limit is None:
        raw_entry = float(window[0].open)
        if (long and not stop < raw_entry < target) or (not long and not target < raw_entry < stop):
            return DecisionOutcome(False, "gapped_past_bracket", None, None, None, 0)
        fill_index, entry, at_open = 0, costs.marketable_fill(raw_entry, buy=long), True
    else:
        day = _bar_date(window[0])
        for k, bar in enumerate(window):
            if _bar_date(bar) != day:
                return DecisionOutcome(False, "limit_not_filled", None, None, None, 0)
            if long and bar.open <= limit:
                fill_index, entry, at_open = k, min(limit, costs.marketable_fill(float(bar.open), buy=True)), True
            elif long and bar.low < limit:
                fill_index, entry = k, float(limit)
            elif not long and bar.open >= limit:
                fill_index, entry, at_open = k, max(limit, costs.marketable_fill(float(bar.open), buy=False)), True
            elif not long and bar.high > limit:
                fill_index, entry = k, float(limit)
            if fill_index is not None:
                break
        if fill_index is None:
            return DecisionOutcome(False, "limit_not_filled", None, None, None, 0,
                                   resolved=len(window) >= max_bars)

    worst = best = 0.0
    for k in range(fill_index, len(window)):
        bar = window[k]
        target_allowed = k > fill_index or at_open
        exit_raw, reason, marketable = None, "", True
        if long:
            if bar.open <= stop and (k > fill_index or at_open):
                exit_raw, reason = float(bar.open), "stop_gap"
            elif bar.low <= stop:
                exit_raw, reason = stop, "stop"
            elif target_allowed and bar.high >= target:
                exit_raw, reason, marketable = target, "target", False
        else:
            if bar.open >= stop and (k > fill_index or at_open):
                exit_raw, reason = float(bar.open), "stop_gap"
            elif bar.high >= stop:
                exit_raw, reason = stop, "stop"
            elif target_allowed and bar.low <= target:
                exit_raw, reason, marketable = target, "target", False
        low, high = float(bar.low), float(bar.high)
        if exit_raw is not None:
            # Clip the exit bar at the exit price: nothing beyond the fill counts as excursion.
            stopped = reason.startswith("stop")
            if long:
                low, high = (max(low, exit_raw), high) if stopped else (low, min(high, exit_raw))
            else:
                low, high = (low, min(high, exit_raw)) if stopped else (max(low, exit_raw), high)
        up, down = (high / entry - 1) * 100, (low / entry - 1) * 100
        adverse, favourable = (down, up) if long else (-up, -down)
        worst, best = min(worst, adverse), max(best, favourable)
        if exit_raw is not None:
            exit_ = costs.marketable_fill(exit_raw, buy=not long) if marketable else exit_raw
            ret = (exit_ / entry - 1) * (1 if long else -1) * 100 - _fee_pct(costs)
            return DecisionOutcome(True, reason, entry, exit_, ret, k - fill_index + 1, worst, best)
    last = window[-1]
    exit_ = costs.marketable_fill(float(last.close), buy=not long)
    ret = (exit_ / entry - 1) * (1 if long else -1) * 100 - _fee_pct(costs)
    return DecisionOutcome(True, "timeout", entry, exit_, ret, len(window) - fill_index, worst, best,
                           resolved=len(window) >= max_bars)


def _bar_date(bar: PriceBar):
    from backtest.metrics import as_datetime

    dt = as_datetime(bar.time)
    return None if dt is None else dt.date()


def _fee_pct(costs: CostModel) -> float:
    """Sell-side regulatory fee in % of price (sells happen on long exit / short entry)."""
    return costs.sell_fee_bps / 100
