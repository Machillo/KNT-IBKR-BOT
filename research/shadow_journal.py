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

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from backtest.costs import BASELINE, CostModel
from market.history import PriceBar


class ShadowJournal:
    def __init__(self, path: str | Path = "state/strategy_performance.db") -> None:
        self.path = Path(path)
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

    def record_decision(self, cycle_id: str, *, symbol: str, con_id: int, bar_time, timeframe: str,
                        regime: str, action: str, strategy: str | None, side: str | None,
                        score: float | None, entry: float | None, stop: float | None,
                        target: float | None, reason: str | None) -> int:
        with sqlite3.connect(self.path) as conn:
            cur = conn.execute(
                """INSERT INTO shadow_decisions (cycle_id, created_at, symbol, con_id, bar_time, timeframe,
                   regime, action, strategy, side, score, entry, stop, target, reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (cycle_id, datetime.now(timezone.utc).isoformat(), symbol, int(con_id or 0),
                 None if bar_time is None else str(bar_time), timeframe, regime, action, strategy, side,
                 score, entry, stop, target, reason),
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


def score_decision(side: str, stop: float, target: float, bars_after: list[PriceBar],
                   costs: CostModel = BASELINE, max_bars: int = 60) -> DecisionOutcome:
    """Outcome of a LONG/SHORT bracket placed at the open of ``bars_after[0]``.

    ``bars_after`` must start with the first bar AFTER the decision bar. Returns net
    return per share (entry/exit costs in bps; commission excluded because it depends on
    size). Unresolved after ``max_bars`` -> exit at that bar's close ("timeout").
    """
    side = side.upper()
    if side not in {"LONG", "SHORT"} or not bars_after:
        return DecisionOutcome(False, "no_data", None, None, None, 0)
    long = side == "LONG"
    raw_entry = float(bars_after[0].open)
    if (long and not stop < raw_entry < target) or (not long and not target < raw_entry < stop):
        return DecisionOutcome(False, "gapped_past_bracket", None, None, None, 0)
    entry = costs.marketable_fill(raw_entry, buy=long)
    for k, bar in enumerate(bars_after[:max_bars]):
        if long:
            if bar.open <= stop:
                raw, reason, marketable = float(bar.open), "stop_gap", True
            elif bar.low <= stop:
                raw, reason, marketable = stop, "stop", True
            elif bar.high >= target:
                raw, reason, marketable = target, "target", False
            else:
                continue
        else:
            if bar.open >= stop:
                raw, reason, marketable = float(bar.open), "stop_gap", True
            elif bar.high >= stop:
                raw, reason, marketable = stop, "stop", True
            elif bar.low <= target:
                raw, reason, marketable = target, "target", False
            else:
                continue
        exit_ = costs.marketable_fill(raw, buy=not long) if marketable else raw
        ret = (exit_ / entry - 1) * (1 if long else -1) * 100
        return DecisionOutcome(True, reason, entry, exit_, ret, k + 1)
    last = bars_after[:max_bars][-1]
    exit_ = costs.marketable_fill(float(last.close), buy=not long)
    ret = (exit_ / entry - 1) * (1 if long else -1) * 100
    return DecisionOutcome(True, "timeout", entry, exit_, ret, len(bars_after[:max_bars]))
