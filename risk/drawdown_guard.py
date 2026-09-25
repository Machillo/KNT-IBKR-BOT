"""Multi-day drawdown lock (sticky across days and restarts).

The daily-loss lock resets every trading date, so a sequence of losing days below the
daily limit could compound without ever stopping entries. This guard tracks a
persisted high-water mark of NetLiquidation per account and locks new entries when
equity falls ``max_drawdown_pct`` below it. The lock persists until a human resets it
explicitly (``run_reset_drawdown_lock.py`` with a literal ACK); it never unlocks itself.

Fail closed: an unreadable/corrupt state file raises, and the supervisor treats that as
a monitoring failure (entries locked).
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

RESET_ACK = "I_REVIEWED_THE_DRAWDOWN_AND_ACCEPT_RESETTING_THE_HIGH_WATER_MARK"


@dataclass(frozen=True)
class DrawdownRecord:
    account: str
    peak_equity: float
    peak_at_utc: str
    locked: bool = False
    lock_reason: str = ""
    locked_at_utc: str = ""


@dataclass(frozen=True)
class DrawdownStatus:
    record: DrawdownRecord
    drawdown_pct: float
    newly_locked: bool


class DrawdownStateStore:
    def __init__(self, path: str | Path = "state/drawdown_state.json") -> None:
        self.path = Path(path)

    def _read(self) -> dict:
        if not self.path.exists():
            return {"version": 1, "records": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Drawdown state unreadable/corrupt: {self.path}; entries must stay locked") from exc
        if data.get("version") != 1 or not isinstance(data.get("records"), dict):
            raise RuntimeError(f"Unsupported drawdown state format: {self.path}")
        return data

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)

    def get(self, account: str) -> DrawdownRecord | None:
        raw = self._read()["records"].get(account)
        if raw is None:
            return None
        try:
            record = DrawdownRecord(**raw)
        except TypeError as exc:
            raise RuntimeError("Invalid drawdown state record") from exc
        if record.peak_equity <= 0:
            raise RuntimeError("Persisted peak equity must be > 0")
        return record

    def put(self, record: DrawdownRecord) -> None:
        data = self._read()
        data["records"][record.account] = asdict(record)
        self._write(data)

    def reset(self, account: str, equity: float, ack: str) -> DrawdownRecord:
        if ack != RESET_ACK:
            raise PermissionError("Drawdown lock reset requires the literal human ACK")
        if equity <= 0:
            raise ValueError("equity must be > 0")
        record = DrawdownRecord(account, float(equity), _now())
        self.put(record)
        return record


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DrawdownGuard:
    def __init__(self, store: DrawdownStateStore, account: str, max_drawdown_pct: float) -> None:
        if not account:
            raise ValueError("account is required")
        if not 0 < max_drawdown_pct <= 1:
            raise ValueError("max_drawdown_pct must be in (0, 1]")
        self.store = store
        self.account = account
        self.max_drawdown_pct = float(max_drawdown_pct)

    def evaluate(self, equity: float) -> DrawdownStatus:
        if equity is None or equity <= 0:
            raise RuntimeError("Drawdown guard requires positive NetLiquidation")
        record = self.store.get(self.account)
        if record is None:
            record = DrawdownRecord(self.account, float(equity), _now())
            self.store.put(record)
        if record.locked:
            dd = max(0.0, (record.peak_equity - equity) / record.peak_equity)
            return DrawdownStatus(record, dd, False)
        if equity > record.peak_equity:
            record = DrawdownRecord(self.account, float(equity), _now())
            self.store.put(record)
        dd = max(0.0, (record.peak_equity - equity) / record.peak_equity)
        if dd >= self.max_drawdown_pct:
            record = DrawdownRecord(
                self.account, record.peak_equity, record.peak_at_utc, True,
                f"multi-day drawdown {dd * 100:.2f}% >= {self.max_drawdown_pct * 100:.2f}% from high-water mark",
                _now(),
            )
            self.store.put(record)
            return DrawdownStatus(record, dd, True)
        return DrawdownStatus(record, dd, False)
