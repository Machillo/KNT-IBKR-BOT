from research.alex_ruiz_lab import candidate_strategies
from market.history import PriceBar
from strategies.momentum import SignalSide


def make_bars(count=140, start=100.0, step=0.2):
    bars=[]
    price=start
    for i in range(count):
        close=price+step
        bars.append(PriceBar(str(i), price, close+0.6, price-0.6, close, 1000+i*5))
        price=close
    return bars


def test_generation_two_grid_is_bounded_and_named_uniquely():
    strategies = candidate_strategies()
    names = [s.name for s in strategies]
    assert len(strategies) == 96
    assert len(set(names)) == len(names)
    assert all(name.startswith("alex2_") for name in names)


def test_generation_two_candidates_evaluate_without_error():
    bars = make_bars()
    for strategy in candidate_strategies():
        signal = strategy.evaluate(bars)
        assert signal.side in {SignalSide.LONG, SignalSide.SHORT, SignalSide.FLAT}
        assert 0 <= signal.score <= 100
