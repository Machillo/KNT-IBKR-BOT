from market.history import PriceBar
from engine.strategy_selector import StrategySelector
from strategies.library import PairsTradingStrategy, SINGLE_ASSET_STRATEGIES
from strategies.momentum import SignalSide


def make_bars(count=100, start=100.0, step=0.5):
    bars=[]
    price=start
    for i in range(count):
        close=price+step
        bars.append(PriceBar(str(i), price, close+0.4, price-0.4, close, 1000+i*10))
        price=close
    return bars


def test_ten_single_asset_plus_pairs_make_eleven_families():
    assert len(SINGLE_ASSET_STRATEGIES) == 10
    assert PairsTradingStrategy.name == "pairs_market_neutral_v1"


def test_all_single_asset_strategies_evaluate_without_error():
    bars=make_bars()
    for factory in SINGLE_ASSET_STRATEGIES:
        signal=factory().evaluate(bars)
        assert signal.side in {SignalSide.LONG, SignalSide.SHORT, SignalSide.FLAT}
        assert 0 <= signal.score <= 100


def test_alex_ruiz_inspired_families_are_registered():
    names = {factory.name for factory in SINGLE_ASSET_STRATEGIES}
    assert "alex_fib_trend_pullback_v1" in names
    assert "alex_liquidity_fib_reversal_v1" in names
    assert "alex_structure_sr_confluence_v1" in names


def test_selector_returns_ranked_evaluations():
    selection=StrategySelector(minimum_score=0).evaluate(make_bars())
    assert len(selection.evaluations) == 10
    assert selection.evaluations[0].adjusted_score >= selection.evaluations[-1].adjusted_score


def test_pairs_strategy_detects_ratio_extension():
    a=make_bars(count=60, start=100, step=0.1)
    b=make_bars(count=60, start=100, step=0.1)
    last=a[-1]
    a[-1]=PriceBar(last.time, last.open, last.high+20, last.low, last.close+20, last.volume)
    signal=PairsTradingStrategy().evaluate_pair(a,b)
    assert signal.side_a in {SignalSide.SHORT, SignalSide.FLAT}
    assert signal.side_b in {SignalSide.LONG, SignalSide.FLAT}
