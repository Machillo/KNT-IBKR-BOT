from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class PersistedDailyRiskState:
    account: str
    trading_date: str
    starting_equity: float
    kill_switch_triggered: bool = False
    trigger_reason: str = ""
    updated_at_utc: str = ""


class DailyRiskStateStore:
    """Small fail-closed JSON store for the sticky daily risk baseline/state."""

    def __init__(self, path: str | Path = "state/risk_state.json") -> None:
        self.path = Path(path)

    @staticmethod
    def _key(account: str, trading_date: str) -> str:
        return f"{account}:{trading_date}"

    def load_or_create(
        self,
        *,
        account: str,
        trading_date: str,
        starting_equity: float,
    ) -> tuple[PersistedDailyRiskState, bool]:
        if not account:
            raise ValueError("account is required")
        if starting_equity <= 0:
            raise ValueError("starting_equity must be > 0")

        data = self._read()
        key = self._key(account, trading_date)
        raw = data.get("records", {}).get(key)
        if raw is not None:
            state = self._decode(raw)
            if state.account != account or state.trading_date != trading_date:
                raise RuntimeError("Persisted daily risk state key/content mismatch")
            return state, False

        state = PersistedDailyRiskState(
            account=account,
            trading_date=trading_date,
            starting_equity=float(starting_equity),
            updated_at_utc=self._now_utc(),
        )
        data.setdefault("records", {})[key] = asdict(state)
        self._write(data)
        return state, True

    def mark_triggered(
        self,
        *,
        account: str,
        trading_date: str,
        reason: str,
    ) -> PersistedDailyRiskState:
        data = self._read()
        key = self._key(account, trading_date)
        raw = data.get("records", {}).get(key)
        if raw is None:
            raise RuntimeError("Cannot mark Kill Switch before daily baseline exists")

        current = self._decode(raw)
        state = PersistedDailyRiskState(
            account=current.account,
            trading_date=current.trading_date,
            starting_equity=current.starting_equity,
            kill_switch_triggered=True,
            trigger_reason=reason or current.trigger_reason or "daily loss limit reached",
            updated_at_utc=self._now_utc(),
        )
        data["records"][key] = asdict(state)
        self._write(data)
        return state

    def _read(self) -> dict:
        if not self.path.exists():
            return {"version": 1, "records": {}}
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"Daily risk state is unreadable/corrupt: {self.path}. "
                "Trading must remain stopped until it is inspected."
            ) from exc
        if data.get("version") != 1 or not isinstance(data.get("records"), dict):
            raise RuntimeError(f"Unsupported/invalid daily risk state format: {self.path}")
        return data

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, sort_keys=True)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except OSError as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise RuntimeError(f"Could not persist daily risk state: {self.path}") from exc

    @staticmethod
    def _decode(raw: dict) -> PersistedDailyRiskState:
        try:
            state = PersistedDailyRiskState(**raw)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Invalid persisted daily risk state record") from exc
        if state.starting_equity <= 0:
            raise RuntimeError("Persisted starting_equity must be > 0")
        return state

    @staticmethod
    def _now_utc() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")
