from datetime import datetime, timezone

from market.session import USStockSessionPolicy


def test_us_stock_session_open_during_regular_weekday_hours():
    policy = USStockSessionPolicy()
    # 2026-09-08 14:00 UTC = 10:00 America/New_York (DST), Tuesday.
    state = policy.state(datetime(2026, 9, 8, 14, 0, tzinfo=timezone.utc))
    assert state.market_open is True
    assert state.session == "REGULAR"


def test_us_stock_session_closed_before_open_and_on_weekend():
    policy = USStockSessionPolicy()
    before_open = policy.state(datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc))
    weekend = policy.state(datetime(2026, 9, 12, 15, 0, tzinfo=timezone.utc))

    assert before_open.market_open is False
    assert before_open.session == "CLOSED"
    assert weekend.market_open is False
    assert weekend.session == "CLOSED"


def test_broker_calendar_policy_fails_closed_until_refreshed_and_honours_holidays():
    import asyncio
    from types import SimpleNamespace

    from market.session import BrokerCalendarSessionPolicy

    tue_10 = datetime(2026, 9, 8, 14, 0, tzinfo=timezone.utc)   # 10:00 NY, regular clock open
    policy = BrokerCalendarSessionPolicy(reference_contract=object())
    assert policy.state(tue_10).market_open is False            # not refreshed -> closed

    class FakeIB:
        def __init__(self, hours):
            self.hours = hours

        async def reqContractDetailsAsync(self, contract):
            return [SimpleNamespace(liquidHours=self.hours, timeZoneId="US/Eastern")]

    open_day = "20260908:0930-20260908:1600;20260909:0930-20260909:1600"
    assert asyncio.run(policy.refresh(FakeIB(open_day), tue_10)) is True
    assert policy.state(tue_10).market_open is True
    # inside the last 15 minutes -> refused
    assert policy.state(datetime(2026, 9, 8, 19, 50, tzinfo=timezone.utc)).market_open is False

    holiday = BrokerCalendarSessionPolicy(reference_contract=object())
    asyncio.run(holiday.refresh(FakeIB("20260908:CLOSED;20260909:0930-20260909:1600"), tue_10))
    assert holiday.state(tue_10).market_open is False
    assert holiday.state(tue_10).session == "BROKER_CALENDAR_CLOSED"


def test_broker_calendar_refresh_failure_keeps_market_closed():
    import asyncio

    from market.session import BrokerCalendarSessionPolicy

    class BrokenIB:
        async def reqContractDetailsAsync(self, contract):
            raise RuntimeError("no details")

    policy = BrokerCalendarSessionPolicy(reference_contract=object())
    now = datetime(2026, 9, 8, 14, 0, tzinfo=timezone.utc)
    assert asyncio.run(policy.refresh(BrokenIB(), now)) is False
    assert policy.state(now).market_open is False
