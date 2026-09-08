from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path
import sqlite3

from backtest.engine import BacktestResult


@dataclass(frozen=True)
class PerformanceRecord:
    symbol: str
    asset_class: str
    timeframe: str
    regime: str
    strategy: str
    split: str
    bars: int
    trades: int
    total_return_pct: float
    max_drawdown_pct: float
    win_rate_pct: float
    profit_factor: float | None
    sharpe: float | None


@dataclass(frozen=True)
class PerformanceEvidence:
    samples: int
    trades: int
    mean_return_pct: float
    mean_drawdown_pct: float
    mean_win_rate_pct: float
    mean_profit_factor: float | None
    mean_sharpe: float | None
    oos_samples: int
    evidence_score: float


class StrategyPerformanceStore:
    """SQLite-backed research memory for strategy performance and learning history."""

    def __init__(self, path: str | Path = "state/strategy_performance.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS strategy_performance (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    symbol TEXT NOT NULL,
                    asset_class TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    regime TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    split TEXT NOT NULL,
                    bars INTEGER NOT NULL,
                    trades INTEGER NOT NULL,
                    total_return_pct REAL NOT NULL,
                    max_drawdown_pct REAL NOT NULL,
                    win_rate_pct REAL NOT NULL,
                    profit_factor REAL,
                    sharpe REAL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_strategy_context
                ON strategy_performance(symbol, asset_class, timeframe, regime, strategy, split)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS research_context (
                    symbol TEXT NOT NULL,
                    asset_class TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    researched_at TEXT NOT NULL,
                    bars INTEGER NOT NULL,
                    PRIMARY KEY(symbol, asset_class, timeframe)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS learning_assessments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    asset_class TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    regime TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    status TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    selector_bonus REAL NOT NULL,
                    freshness_factor REAL NOT NULL,
                    reason TEXT NOT NULL,
                    evidence_score REAL,
                    samples INTEGER,
                    trades INTEGER,
                    oos_samples INTEGER
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_learning_context
                ON learning_assessments(symbol, asset_class, timeframe, regime, strategy, id)
                """
            )

    def clear_context(self, *, symbol: str, asset_class: str, timeframe: str) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM strategy_performance WHERE symbol=? AND asset_class=? AND timeframe=?",
                (symbol.upper(), asset_class.upper(), timeframe),
            )
            return int(cursor.rowcount or 0)

    def mark_researched(self, *, symbol: str, asset_class: str, timeframe: str, bars: int) -> None:
        researched_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO research_context(symbol, asset_class, timeframe, researched_at, bars)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(symbol, asset_class, timeframe)
                DO UPDATE SET researched_at=excluded.researched_at, bars=excluded.bars
                """,
                (symbol.upper(), asset_class.upper(), timeframe, researched_at, int(bars)),
            )

    def research_status(self, *, symbol: str, asset_class: str, timeframe: str) -> sqlite3.Row | None:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM research_context WHERE symbol=? AND asset_class=? AND timeframe=?",
                (symbol.upper(), asset_class.upper(), timeframe),
            ).fetchone()

    def record_learning_assessment(
        self,
        *,
        symbol: str,
        asset_class: str,
        timeframe: str,
        regime: str,
        strategy: str,
        status: str,
        confidence: float,
        selector_bonus: float,
        freshness_factor: float,
        reason: str,
        evidence: PerformanceEvidence | None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO learning_assessments (
                    created_at, symbol, asset_class, timeframe, regime, strategy,
                    status, confidence, selector_bonus, freshness_factor, reason,
                    evidence_score, samples, trades, oos_samples
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now(timezone.utc).isoformat(),
                    symbol.upper(), asset_class.upper(), timeframe, regime, strategy,
                    status, float(confidence), float(selector_bonus), float(freshness_factor), reason,
                    None if evidence is None else float(evidence.evidence_score),
                    None if evidence is None else int(evidence.samples),
                    None if evidence is None else int(evidence.trades),
                    None if evidence is None else int(evidence.oos_samples),
                ),
            )

    def learning_history(
        self,
        *,
        symbol: str,
        asset_class: str,
        timeframe: str,
        regime: str,
        strategy: str,
        limit: int = 50,
    ) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                """
                SELECT * FROM learning_assessments
                WHERE symbol=? AND asset_class=? AND timeframe=? AND regime=? AND strategy=?
                ORDER BY id DESC LIMIT ?
                """,
                (symbol.upper(), asset_class.upper(), timeframe, regime, strategy, int(limit)),
            ).fetchall()

    def record(self, item: PerformanceRecord) -> None:
        pf = item.profit_factor if item.profit_factor is None or isfinite(item.profit_factor) else None
        sharpe = item.sharpe if item.sharpe is None or isfinite(item.sharpe) else None
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO strategy_performance (
                    symbol, asset_class, timeframe, regime, strategy, split, bars, trades,
                    total_return_pct, max_drawdown_pct, win_rate_pct, profit_factor, sharpe
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.symbol.upper(), item.asset_class.upper(), item.timeframe,
                    item.regime, item.strategy, item.split.upper(), item.bars, item.trades,
                    item.total_return_pct, item.max_drawdown_pct, item.win_rate_pct,
                    pf, sharpe,
                ),
            )

    def record_result(
        self,
        *, symbol: str, asset_class: str, timeframe: str, regime: str,
        strategy: str, split: str, bars: int, result: BacktestResult,
    ) -> None:
        self.record(PerformanceRecord(
            symbol=symbol, asset_class=asset_class, timeframe=timeframe, regime=regime,
            strategy=strategy, split=split, bars=bars, trades=result.trades,
            total_return_pct=result.total_return_pct,
            max_drawdown_pct=result.max_drawdown_pct,
            win_rate_pct=result.win_rate_pct,
            profit_factor=result.profit_factor,
            sharpe=result.sharpe,
        ))

    def evidence(
        self, *, symbol: str, asset_class: str, timeframe: str,
        regime: str, strategy: str,
    ) -> PerformanceEvidence | None:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM strategy_performance
                WHERE symbol=? AND asset_class=? AND timeframe=? AND regime=? AND strategy=?
                ORDER BY id DESC LIMIT 100
                """,
                (symbol.upper(), asset_class.upper(), timeframe, regime, strategy),
            ).fetchall()
        if not rows:
            return None

        trades = sum(int(r["trades"]) for r in rows)
        weights = [max(1, int(r["trades"])) for r in rows]

        def weighted(key: str) -> float:
            usable = [(r, w) for r, w in zip(rows, weights) if r[key] is not None]
            return sum(float(r[key]) * w for r, w in usable) / max(1, sum(w for _, w in usable))

        mean_return = weighted("total_return_pct")
        mean_dd = weighted("max_drawdown_pct")
        mean_win = weighted("win_rate_pct")
        pf = weighted("profit_factor") if any(r["profit_factor"] is not None for r in rows) else None
        sharpe = weighted("sharpe") if any(r["sharpe"] is not None for r in rows) else None
        oos_samples = sum(1 for r in rows if str(r["split"]).upper() == "OOS")

        score = 0.0
        score += max(-10.0, min(10.0, mean_return / 2.0))
        score += max(-8.0, min(8.0, ((pf or 1.0) - 1.0) * 10.0))
        score += max(-6.0, min(6.0, (sharpe or 0.0) * 3.0))
        score -= max(0.0, min(8.0, mean_dd / 5.0))
        score *= min(1.0, trades / 80.0)
        if oos_samples == 0:
            score *= 0.5
        score = max(-20.0, min(20.0, score))

        return PerformanceEvidence(
            samples=len(rows), trades=trades, mean_return_pct=mean_return,
            mean_drawdown_pct=mean_dd, mean_win_rate_pct=mean_win,
            mean_profit_factor=pf, mean_sharpe=sharpe,
            oos_samples=oos_samples, evidence_score=score,
        )

    def leaderboard(
        self, *, asset_class: str | None = None, timeframe: str | None = None,
        limit: int = 50,
    ) -> list[sqlite3.Row]:
        clauses: list[str] = []
        params: list[object] = []
        if asset_class:
            clauses.append("asset_class=?")
            params.append(asset_class.upper())
        if timeframe:
            clauses.append("timeframe=?")
            params.append(timeframe)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"""
            SELECT symbol, asset_class, timeframe, regime, strategy,
                   COUNT(*) AS samples, SUM(trades) AS trades,
                   AVG(total_return_pct) AS avg_return,
                   AVG(max_drawdown_pct) AS avg_dd,
                   AVG(profit_factor) AS avg_pf,
                   AVG(sharpe) AS avg_sharpe,
                   SUM(CASE WHEN split='OOS' THEN 1 ELSE 0 END) AS oos_samples
            FROM strategy_performance
            {where}
            GROUP BY symbol, asset_class, timeframe, regime, strategy
            ORDER BY (AVG(total_return_pct) - 0.5 * AVG(max_drawdown_pct)) DESC
            LIMIT ?
        """
        params.append(limit)
        with self._connect() as conn:
            return conn.execute(query, params).fetchall()
