from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from portfolio.state import PortfolioState


@dataclass(frozen=True)
class PortfolioSnapshotRecord:
    created_at: str
    account: str
    net_liquidation: float
    cash: float
    committed_notional: float
    pending_order_notional: float
    gross_exposure_pct: float
    daily_loss_used: float
    daily_loss_limit: float
    trading_locked: bool
    position_count: int
    pending_order_count: int


class PortfolioHistoryStore:
    """Append-only portfolio state audit trail for Paper/Shadow cycles."""

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
                CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    account TEXT NOT NULL,
                    net_liquidation REAL NOT NULL,
                    cash REAL NOT NULL,
                    committed_notional REAL NOT NULL,
                    pending_order_notional REAL NOT NULL,
                    gross_exposure_pct REAL NOT NULL,
                    daily_loss_used REAL NOT NULL,
                    daily_loss_limit REAL NOT NULL,
                    trading_locked INTEGER NOT NULL,
                    position_count INTEGER NOT NULL,
                    pending_order_count INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """CREATE INDEX IF NOT EXISTS idx_portfolio_snapshots_account_time
                ON portfolio_snapshots(account, id)"""
            )

    def record(self, *, account: str, state: PortfolioState) -> None:
        snapshot = state.snapshot
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO portfolio_snapshots (
                    created_at, account, net_liquidation, cash, committed_notional,
                    pending_order_notional, gross_exposure_pct, daily_loss_used,
                    daily_loss_limit, trading_locked, position_count, pending_order_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now(timezone.utc).isoformat(),
                    account,
                    snapshot.net_liquidation,
                    snapshot.cash,
                    snapshot.committed_notional,
                    snapshot.pending_order_notional,
                    snapshot.gross_exposure_pct,
                    snapshot.daily_loss_used,
                    snapshot.daily_loss_limit,
                    1 if snapshot.trading_locked else 0,
                    len(state.positions),
                    len(state.pending_orders),
                ),
            )

    def recent(self, account: str, limit: int = 50) -> list[PortfolioSnapshotRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM portfolio_snapshots
                WHERE account=? ORDER BY id DESC LIMIT ?
                """,
                (account, int(limit)),
            ).fetchall()
        return [
            PortfolioSnapshotRecord(
                created_at=str(row["created_at"]),
                account=str(row["account"]),
                net_liquidation=float(row["net_liquidation"]),
                cash=float(row["cash"]),
                committed_notional=float(row["committed_notional"]),
                pending_order_notional=float(row["pending_order_notional"]),
                gross_exposure_pct=float(row["gross_exposure_pct"]),
                daily_loss_used=float(row["daily_loss_used"]),
                daily_loss_limit=float(row["daily_loss_limit"]),
                trading_locked=bool(row["trading_locked"]),
                position_count=int(row["position_count"]),
                pending_order_count=int(row["pending_order_count"]),
            )
            for row in rows
        ]
