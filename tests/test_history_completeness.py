from datetime import datetime, timedelta, timezone

from market.history import PriceBar, bar_size_seconds, drop_incomplete_last_bar

NOW = datetime(2026, 9, 24, 15, 30, tzinfo=timezone.utc)


def bar(t):
    return PriceBar(t, 1, 1, 1, 1, 1)


def test_bar_size_parsing():
    assert bar_size_seconds("1 hour") == 3600
    assert bar_size_seconds("4 hours") == 14400
    assert bar_size_seconds("15 mins") == 900
    assert bar_size_seconds("1 day") == 86400
    assert bar_size_seconds("weird") is None


def test_forming_bar_is_dropped():
    bars = [bar(NOW - timedelta(hours=2)), bar(NOW - timedelta(minutes=30))]
    assert drop_incomplete_last_bar(bars, "1 hour", NOW) == bars[:1]


def test_completed_bar_is_kept():
    bars = [bar(NOW - timedelta(hours=2)), bar(NOW - timedelta(hours=1))]
    assert drop_incomplete_last_bar(bars, "1 hour", NOW) == bars


def test_unprovable_completion_fails_safe_by_dropping():
    naive = [bar(datetime(2026, 9, 24, 9, 30)), bar(datetime(2026, 9, 24, 10, 30))]
    assert drop_incomplete_last_bar(naive, "1 hour", NOW) == naive[:1]
    as_text = [bar("20260924"), bar("20260925")]
    assert drop_incomplete_last_bar(as_text, "1 day", NOW) == as_text[:1]
    assert drop_incomplete_last_bar([], "1 hour", NOW) == []
