from datetime import datetime, timedelta

from market.history import PriceBar
from research.null_models import RandomLongStrategy
from strategies.momentum import SignalSide


def bars(n=60):
    t0 = datetime(2024, 1, 2)
    return [PriceBar(t0 + timedelta(days=i), 100 + i, 101 + i, 99 + i, 100.5 + i, 1e6) for i in range(n)]


def test_random_long_is_deterministic_and_long_only():
    a, b = RandomLongStrategy(7, p=0.5), RandomLongStrategy(7, p=0.5)
    data = bars()
    sa = [a.evaluate(data[:k]).side for k in range(31, 60)]
    sb = [b.evaluate(data[:k]).side for k in range(31, 60)]
    assert sa == sb
    assert set(sa) <= {SignalSide.LONG, SignalSide.FLAT} and SignalSide.LONG in sa


def test_random_long_uses_valid_bracket_geometry():
    s = RandomLongStrategy(1, p=1.0).evaluate(bars())
    assert s.stop < s.entry < s.target
