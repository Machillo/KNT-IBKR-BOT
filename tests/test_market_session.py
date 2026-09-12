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
