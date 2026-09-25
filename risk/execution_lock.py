"""Persistent execution lock: set when the broker state became uncertain after a transmission
(partial bracket, unwind failure, flattened partial fill, guard refusal mid-bracket, journal
failure after transmit). Unlike the in-memory RiskManager lock it survives restarts and the
next trading day; only a human clears it (``run_reset_execution_lock.py`` with an ACK, after
checking the broker). An unreadable file counts as locked (fail closed).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from config.config import state_path

RESET_ACK = "I_CHECKED_THE_BROKER_AND_CLEAR_THE_EXECUTION_LOCK"


class ExecutionLockStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else state_path("execution_lock.json")

    def read(self) -> dict | None:
        """None when unlocked; otherwise the lock record (unreadable -> treated as locked)."""
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"reason": "execution lock file unreadable", "events": []}
        return data if isinstance(data, dict) else {"reason": "execution lock file invalid", "events": []}

    def set(self, reason: str) -> None:
        current = self.read() or {"reason": reason, "created_at_utc": _now(), "events": []}
        current.setdefault("events", []).append({"at_utc": _now(), "reason": reason})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(current, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)

    def clear(self, ack: str) -> None:
        if ack != RESET_ACK:
            raise PermissionError("execution lock reset requires the literal ACK")
        self.path.unlink(missing_ok=True)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
