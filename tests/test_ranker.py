from types import SimpleNamespace

from core.market_data import MarketSnapshot
from market.models import DiscoveryCandidate
from market.ranker import LiquidityRanker


def candidate(rank=0, symbol="ABC"):
    return DiscoveryCandidate(rank, symbol, "STK", "NASDAQ", "USD", SimpleNamespace())


def test_tight_spread_candidate_is_eligible():
    snap = MarketSnapshot("ABC", 99.99, 100.01, 100.0, 100.0, 100000)
    item = LiquidityRanker().evaluate(candidate(), snap)
    assert item.eligible
    assert item.score > 0
    assert item.spread_bps is not None


def test_wide_spread_candidate_is_rejected():
    snap = MarketSnapshot("ABC", 99.0, 101.0, 100.0, 100.0, 100000)
    item = LiquidityRanker(max_spread_bps=50).evaluate(candidate(), snap)
    assert not item.eligible
    assert "spread" in item.reason


def test_lower_scanner_rank_scores_better_when_spread_equal():
    ranker = LiquidityRanker()
    snap = MarketSnapshot("ABC", 99.99, 100.01, 100.0, 100.0, 100000)
    first = ranker.evaluate(candidate(rank=0, symbol="AAA"), snap)
    later = ranker.evaluate(candidate(rank=10, symbol="BBB"), snap)
    assert first.score > later.score
