from market.history import PriceBar
from strategies.momentum import MomentumStrategy, SignalSide


def bars_from(closes):
    return [PriceBar(str(i), c - 0.5, c + 1, c - 1, c, 1000) for i, c in enumerate(closes)]


def test_momentum_long_on_rising_series():
    strategy = MomentumStrategy(fast=3, slow=6, atr_period=3)
    signal = strategy.evaluate(bars_from([10, 10, 11, 12, 13, 14, 15, 16, 17]))
    assert signal.side == SignalSide.LONG
    assert signal.stop < signal.entry < signal.target


def test_momentum_flat_without_history():
    signal = MomentumStrategy().evaluate(bars_from([10, 11, 12]))
    assert signal.side == SignalSide.FLAT
    assert signal.reason == "insufficient_history"
