from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import csv
import sqlite3

from backtest.analysis import analyze_contextual_validation_csv


@dataclass(frozen=True)
class ContextPromotion:
    scope: str
    context: str
    strategy: str
    status: str
    score: float
    datasets: int
    profitable_pct: float
    stress_survival_pct: float
    avg_return_pct: float
    avg_drawdown_pct: float
    avg_profit_factor: float


class ContextPromotionStore:
    """Append-only contextual strategy promotions derived from validation reports.

    These records can influence strategy confidence but never broker permissions,
    kill-switches, live authorization, or hard risk limits.
    """

    def __init__(self, path: str | Path = "state/strategy_performance.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS strategy_context_promotions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    context TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    status TEXT NOT NULL,
                    score REAL NOT NULL,
                    datasets INTEGER NOT NULL,
                    profitable_pct REAL NOT NULL,
                    stress_survival_pct REAL NOT NULL,
                    avg_return_pct REAL NOT NULL,
                    avg_drawdown_pct REAL NOT NULL,
                    avg_profit_factor REAL NOT NULL,
                    source TEXT NOT NULL DEFAULT 'BACKTEST_VALIDATION'
                )
                """
            )
            conn.execute(
                """CREATE INDEX IF NOT EXISTS idx_context_promotions
                ON strategy_context_promotions(scope, context, strategy, id)"""
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def record(self, item: ContextPromotion) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO strategy_context_promotions (
                    created_at, scope, context, strategy, status, score, datasets,
                    profitable_pct, stress_survival_pct, avg_return_pct,
                    avg_drawdown_pct, avg_profit_factor
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now(timezone.utc).isoformat(), item.scope, item.context,
                    item.strategy, item.status, float(item.score), int(item.datasets),
                    float(item.profitable_pct), float(item.stress_survival_pct),
                    float(item.avg_return_pct), float(item.avg_drawdown_pct),
                    float(item.avg_profit_factor),
                ),
            )
            return int(cursor.lastrowid)

    def latest(self, *, scope: str, context: str, strategy: str) -> sqlite3.Row | None:
        with self._connect() as conn:
            return conn.execute(
                """
                SELECT * FROM strategy_context_promotions
                WHERE scope=? AND context=? AND strategy=?
                ORDER BY id DESC LIMIT 1
                """,
                (scope, context, strategy),
            ).fetchone()

    def latest_best(
        self,
        *,
        strategy: str,
        symbol: str | None = None,
        universe: str | None = None,
        timeframe: str | None = None,
        horizon: str | None = None,
    ) -> sqlite3.Row | None:
        # Specificity order: symbol > universe > horizon > timeframe. Global backtest
        # AVOID is deliberately excluded here; exact live research evidence remains
        # the final authority in LearningEngine.
        candidates = (
            ("symbol", symbol),
            ("universe", universe),
            ("horizon", horizon),
            ("timeframe", timeframe),
        )
        for scope, context in candidates:
            if not context:
                continue
            row = self.latest(scope=scope, context=context, strategy=strategy)
            if row is not None:
                return row
        return None


def import_validation_promotions(
    report_path: str | Path = "reports/backtests/ALL_RESULTS.csv",
    *,
    db_path: str | Path = "state/strategy_performance.db",
) -> int:
    path = Path(report_path)
    if not path.exists():
        raise FileNotFoundError(path)
    contexts = analyze_contextual_validation_csv(path)
    store = ContextPromotionStore(db_path)
    count = 0
    for scope, rows in contexts.items():
        for row in rows:
            store.record(ContextPromotion(
                scope=scope,
                context=row.context,
                strategy=row.strategy,
                status=row.status,
                score=row.score,
                datasets=row.datasets,
                profitable_pct=row.profitable_pct,
                stress_survival_pct=row.stress_survival_pct,
                avg_return_pct=row.avg_return_pct,
                avg_drawdown_pct=row.avg_drawdown_pct,
                avg_profit_factor=row.avg_profit_factor,
            ))
            count += 1
    return count
