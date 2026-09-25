from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
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


def parse_liquid_hours(text: str, tz: ZoneInfo) -> list[tuple[datetime, datetime]]:
    """Parse IBKR ``liquidHours`` ("20260924:0930-20260924:1600;20260926:CLOSED;...")."""
    sessions: list[tuple[datetime, datetime]] = []
    for chunk in (text or "").split(";"):
        chunk = chunk.strip()
        if not chunk or chunk.endswith(":CLOSED") or "-" not in chunk:
            continue
        start_raw, end_raw = chunk.split("-", 1)
        try:
            start = datetime.strptime(start_raw, "%Y%m%d:%H%M").replace(tzinfo=tz)
            end = datetime.strptime(end_raw, "%Y%m%d:%H%M").replace(tzinfo=tz)
        except ValueError:
            continue
        if end > start:
            sessions.append((start, end))
    return sessions


class BrokerCalendarSessionPolicy:
    """Regular-hours clock AND IBKR's own liquid-hours calendar (holidays, half days).

    ``refresh(ib)`` reads contract details for a reference stock (read-only). Until it
    has succeeded for the current exchange date the market is reported CLOSED (fail
    closed). Entries are also refused in the first/last minutes of a session.
    """

    def __init__(self, reference_contract=None, *, base: USStockSessionPolicy | None = None,
                 open_buffer_minutes: int = 5, close_buffer_minutes: int = 15) -> None:
        self.reference_contract = reference_contract
        self.base = base or USStockSessionPolicy()
        self.open_buffer = timedelta(minutes=max(0, open_buffer_minutes))
        self.close_buffer = timedelta(minutes=max(0, close_buffer_minutes))
        self.tz = self.base.timezone
        self.sessions: list[tuple[datetime, datetime]] = []
        self.refreshed_for = None

    def load(self, liquid_hours: str, time_zone_id: str | None, today) -> None:
        try:
            tz = ZoneInfo(time_zone_id) if time_zone_id else self.base.timezone
        except Exception:
            tz = self.base.timezone
        self.tz = tz
        self.sessions = parse_liquid_hours(liquid_hours, tz)
        self.refreshed_for = today

    async def refresh(self, ib, now: datetime | None = None) -> bool:
        today = self.base.state(now).local_time.date()
        if self.refreshed_for == today:
            return True
        contract = self.reference_contract
        if contract is None:
            from ib_async import Stock  # local import keeps the policy testable offline
            contract = Stock("SPY", "SMART", "USD")
        try:
            details = await ib.reqContractDetailsAsync(contract)
        except Exception:
            return False
        if not details:
            return False
        detail = details[0]
        self.load(getattr(detail, "liquidHours", "") or "", getattr(detail, "timeZoneId", None), today)
        return True

    def state(self, now: datetime | None = None) -> MarketSessionState:
        base = self.base.state(now)
        if self.refreshed_for != base.local_time.date():
            return MarketSessionState(False, "CALENDAR_UNVERIFIED", base.checked_at_utc, base.local_time)
        if not base.market_open:
            return base
        current = base.checked_at_utc
        for start, end in self.sessions:
            if start + self.open_buffer <= current < end - self.close_buffer:
                return base
        return MarketSessionState(False, "BROKER_CALENDAR_CLOSED", base.checked_at_utc, base.local_time)
