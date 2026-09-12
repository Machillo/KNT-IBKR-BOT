from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3


@dataclass(frozen=True)
class ResearchSchedulerState:
    symbol: str
    asset_class: str
    timeframe: str
    consecutive_failures: int
    last_status: str | None
    last_reason: str | None
    last_attempt_at: str | None
    next_retry_at: str | None


class ResearchSchedulerStateStore:
    """Persistent scheduler attempt/backoff state stored beside KNT research memory."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS research_scheduler_state (
                    symbol TEXT NOT NULL,
                    asset_class TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    last_status TEXT,
                    last_reason TEXT,
                    last_attempt_at TEXT,
                    next_retry_at TEXT,
                    PRIMARY KEY(symbol, asset_class, timeframe)
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def get(self, *, symbol: str, asset_class: str, timeframe: str) -> ResearchSchedulerState | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM research_scheduler_state
                WHERE symbol=? AND asset_class=? AND timeframe=?
                """,
                (symbol.upper(), asset_class.upper(), timeframe),
            ).fetchone()
        if row is None:
            return None
        return ResearchSchedulerState(
            symbol=row["symbol"], asset_class=row["asset_class"], timeframe=row["timeframe"],
            consecutive_failures=int(row["consecutive_failures"]),
            last_status=row["last_status"], last_reason=row["last_reason"],
            last_attempt_at=row["last_attempt_at"], next_retry_at=row["next_retry_at"],
        )

    def in_backoff(self, *, symbol: str, asset_class: str, timeframe: str) -> bool:
        state = self.get(symbol=symbol, asset_class=asset_class, timeframe=timeframe)
        if state is None or not state.next_retry_at:
            return False
        try:
            retry_at = datetime.fromisoformat(state.next_retry_at)
        except ValueError:
            return False
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) < retry_at

    def record_success(self, *, symbol: str, asset_class: str, timeframe: str, reason: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO research_scheduler_state (
                    symbol, asset_class, timeframe, consecutive_failures,
                    last_status, last_reason, last_attempt_at, next_retry_at
                ) VALUES (?, ?, ?, 0, 'REFRESHED', ?, ?, NULL)
                ON CONFLICT(symbol, asset_class, timeframe) DO UPDATE SET
                    consecutive_failures=0,
                    last_status='REFRESHED',
                    last_reason=excluded.last_reason,
                    last_attempt_at=excluded.last_attempt_at,
                    next_retry_at=NULL
                """,
                (symbol.upper(), asset_class.upper(), timeframe, reason, now),
            )

    def record_failure(
        self, *, symbol: str, asset_class: str, timeframe: str,
        status: str, reason: str, base_backoff_minutes: int = 30,
        max_backoff_hours: int = 24,
    ) -> ResearchSchedulerState:
        previous = self.get(symbol=symbol, asset_class=asset_class, timeframe=timeframe)
        failures = 1 if previous is None else previous.consecutive_failures + 1
        minutes = max(1, int(base_backoff_minutes)) * (2 ** max(0, failures - 1))
        minutes = min(minutes, max(1, int(max_backoff_hours)) * 60)
        now_dt = datetime.now(timezone.utc)
        retry_at = now_dt + timedelta(minutes=minutes)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO research_scheduler_state (
                    symbol, asset_class, timeframe, consecutive_failures,
                    last_status, last_reason, last_attempt_at, next_retry_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, asset_class, timeframe) DO UPDATE SET
                    consecutive_failures=excluded.consecutive_failures,
                    last_status=excluded.last_status,
                    last_reason=excluded.last_reason,
                    last_attempt_at=excluded.last_attempt_at,
                    next_retry_at=excluded.next_retry_at
                """,
                (
                    symbol.upper(), asset_class.upper(), timeframe, failures,
                    status.upper(), reason, now_dt.isoformat(), retry_at.isoformat(),
                ),
            )
        return self.get(symbol=symbol, asset_class=asset_class, timeframe=timeframe)  # type: ignore[return-value]
