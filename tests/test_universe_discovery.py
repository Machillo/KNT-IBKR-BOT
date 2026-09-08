from market.intelligence import MarketIntelligenceService
from market.models import DiscoveryCandidate
from market.universe import US_STOCK_OPPORTUNITY_UNIVERSE


class Contract:
    def __init__(self, con_id: int):
        self.conId = con_id


def test_universe_uses_multiple_dynamic_scanners_without_tickers():
    universe = US_STOCK_OPPORTUNITY_UNIVERSE
    assert len(universe.scanners) == 4
    assert {x.scan_code for x in universe.scanners} == {
        "MOST_ACTIVE", "TOP_PERC_GAIN", "TOP_PERC_LOSE", "HOT_BY_VOLUME"
    }
    assert all(x.instrument == "STK" for x in universe.scanners)
    assert all(x.location_code == "STK.US.MAJOR" for x in universe.scanners)


def test_discovery_shortlist_respects_quote_budget_and_rank():
    candidates = [
        DiscoveryCandidate(rank=rank, symbol=f"S{rank}", sec_type="STK", exchange="SMART", currency="USD", contract=Contract(rank))
        for rank in [8, 1, 5, 2, 7]
    ]
    shortlisted = MarketIntelligenceService._discovery_shortlist(candidates, 3)
    assert [x.rank for x in shortlisted] == [1, 2, 5]


def test_discovery_shortlist_rejects_ambiguous_stock_wrappers():
    candidates = [
        DiscoveryCandidate(rank=0, symbol="RIV RT", sec_type="STK", exchange="SMART", currency="USD", contract=Contract(1)),
        DiscoveryCandidate(rank=1, symbol="EP PRC", sec_type="STK", exchange="SMART", currency="USD", contract=Contract(2)),
        DiscoveryCandidate(rank=2, symbol="NVDA", sec_type="STK", exchange="SMART", currency="USD", contract=Contract(3)),
    ]
    shortlisted = MarketIntelligenceService._discovery_shortlist(candidates, 10)
    assert [x.symbol for x in shortlisted] == ["NVDA"]
