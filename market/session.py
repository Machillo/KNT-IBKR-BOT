from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class MarketSessionState:
    market_open: bool
    session: str
    checked_at_utc: datetime
    local_time: datetime


class USStockSessionPolicy:
    """Simple replaceable policy for the current US-stock shadow universe.

    This policy intentionally lives outside the research scheduler so future
    asset-class session policies (FX, futures, options, international stocks)
    can replace or compose with it without changing scheduler internals.

    It models regular US equity hours only (09:30-16:00 America/New_York,
    Monday-Friday). Exchange-holiday awareness can be upgraded later through
    broker/exchange calendars without changing callers.
    """

    timezone = ZoneInfo("America/New_York")
    regular_open = time(9, 30)
    regular_close = time(16, 0)

    def state(self, now: datetime | None = None) -> MarketSessionState:
        checked_at = now or datetime.now(timezone.utc)
        if checked_at.tzinfo is None:
            checked_at = checked_at.replace(tzinfo=timezone.utc)
        checked_at = checked_at.astimezone(timezone.utc)
        local = checked_at.astimezone(self.timezone)
        weekday_open = local.weekday() < 5
        clock_open = self.regular_open <= local.time() < self.regular_close
        market_open = weekday_open and clock_open
        session = "REGULAR" if market_open else "CLOSED"
        return MarketSessionState(
            market_open=market_open,
            session=session,
            checked_at_utc=checked_at,
            local_time=local,
        )
