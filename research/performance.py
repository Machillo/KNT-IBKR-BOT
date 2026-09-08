from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sqlite3
from typing import Iterable

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
    """SQLite-backed research memory for strategy performance.

    Records are intentionally contextual: symbol + asset class + timeframe + regime + strategy
    + split (TRAIN/OOS). The selector consumes aggregate evidence but raw observations remain
    auditable in SQLite.
    """

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

    def record(self, item: PerformanceRecord) -> None:
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
                    item.profit_factor, item.sharpe,
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
        total_w = sum(weights)
        weighted = lambda key: sum(float(r[key]) * w for r, w in zip(rows, weights) if r[key] is not None) / max(1, sum(w for r, w in zip(rows, weights) if r[key] is not None))

        mean_return = weighted("total_return_pct")
        mean_dd = weighted("max_drawdown_pct")
        mean_win = weighted("win_rate_pct")
        pf = weighted("profit_factor") if any(r["profit_factor"] is not None for r in rows) else None
        sharpe = weighted("sharpe") if any(r["sharpe"] is not None for r in rows) else None
        oos_samples = sum(1 for r in rows if str(r["split"]).upper() == "OOS")

        # Conservative evidence score, centered on zero and capped. OOS and sample depth matter.
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
            ORDER BY avg_return DESC
            LIMIT ?
        """
        params.append(limit)
        with self._connect() as conn:
            return conn.execute(query, params).fetchall()
