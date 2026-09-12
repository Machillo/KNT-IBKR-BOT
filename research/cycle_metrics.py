from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3


@dataclass(frozen=True)
class ResearchCycleMetric:
    id: int
    created_at: str
    mode: str
    considered: int
    attempted: int
    refreshed: int
    skipped: int
    budget: int


class ResearchCycleMetricsStore:
    """Append-only scheduler cycle telemetry stored beside KNT research memory."""

    def __init__(self, path: str | Path) -> None:
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
                CREATE TABLE IF NOT EXISTS research_cycle_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    considered INTEGER NOT NULL,
                    attempted INTEGER NOT NULL,
                    refreshed INTEGER NOT NULL,
                    skipped INTEGER NOT NULL,
                    budget INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """CREATE INDEX IF NOT EXISTS idx_research_cycle_metrics_created
                ON research_cycle_metrics(created_at, id)"""
            )

    def record(
        self,
        *,
        mode: str,
        considered: int,
        attempted: int,
        refreshed: int,
        skipped: int,
        budget: int,
    ) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO research_cycle_metrics (
                    created_at, mode, considered, attempted, refreshed, skipped, budget
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now(timezone.utc).isoformat(), mode.upper(), int(considered),
                    int(attempted), int(refreshed), int(skipped), int(budget),
                ),
            )
            return int(cursor.lastrowid)

    def recent(self, limit: int = 50) -> list[ResearchCycleMetric]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM research_cycle_metrics ORDER BY id DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        return [
            ResearchCycleMetric(
                id=int(row["id"]), created_at=str(row["created_at"]), mode=str(row["mode"]),
                considered=int(row["considered"]), attempted=int(row["attempted"]),
                refreshed=int(row["refreshed"]), skipped=int(row["skipped"]),
                budget=int(row["budget"]),
            )
            for row in rows
        ]
