"""Round-3 families must be causal: a signal at bar k cannot change when future bars change."""
from datetime import datetime, timedelta
from random import Random

import pytest

from market.history import PriceBar
from research.event_study import StudyContext
from research.families import FAMILIES, make_signal

D0 = datetime(2015, 1, 2)


def cohort(seed, n=420, symbols=("SPY", "AAA", "BBB", "CCC", "DDD", "EEE", "FFF")):
    rng = Random(seed)
    data = {}
    for s in symbols:
        price, bars = 100.0, []
        for i in range(n):
            o = price * (1 + rng.gauss(0, 0.01))
            c = o * (1 + rng.gauss(0.0003, 0.015))
            bars.append(PriceBar((D0 + timedelta(days=i)).strftime("%Y-%m-%d"), o, max(o, c) * 1.005,
                                 min(o, c) * 0.995, c, rng.uniform(5e5, 2e6)))
            price = c
        data[s] = bars
    return data


def perturb_future(data, k):
    """Same history up to bar k, completely different bars afterwards."""
    other = cohort(seed=999)
    return {s: bars[:k + 1] + other[s][k + 1:] for s, bars in data.items()}


@pytest.mark.parametrize("name", sorted(FAMILIES))
def test_family_signals_do_not_depend_on_future_bars(name):
    data = cohort(seed=1)
    for k in (300, 360):
        changed = perturb_future(data, k)
        a_signal, _ = make_signal(name)
        b_signal, _ = make_signal(name)
        ctx_a, ctx_b = StudyContext(data), StudyContext(changed)
        for symbol in data:
            assert a_signal(symbol, data[symbol][:k + 1], ctx_a) == b_signal(symbol, changed[symbol][:k + 1], ctx_b), (name, symbol, k)
