import asyncio
from types import SimpleNamespace

from core.market_data import MarketSnapshot
from engine.shadow import ShadowTradingEngine


class FakeMarketData:
    def __init__(self, snapshot, configured=1):
        self.snapshot = snapshot
        self.settings = SimpleNamespace(market_data_type=configured)

    async def snapshot_contract(self, contract, symbol, timeout=3.0):
        if isinstance(self.snapshot, Exception):
            raise self.snapshot
        return self.snapshot


def reference(snapshot, configured=1):
    shadow = ShadowTradingEngine.__new__(ShadowTradingEngine)
    shadow.intelligence = SimpleNamespace(market_data=FakeMarketData(snapshot, configured))
    candidate = SimpleNamespace(contract=object(), symbol="AAPL")
    return asyncio.run(shadow._fresh_reference(candidate))


def snap(bid, ask, last=None, data_type=1):
    return MarketSnapshot("AAPL", bid, ask, last, last, market_data_type=data_type)


def test_two_sided_live_quote_gives_mid():
    assert reference(snap(99.0, 101.0)) == (100.0, 1)


def test_one_sided_quote_gives_no_reference():
    assert reference(snap(None, None, last=100.0))[0] is None


def test_ticker_fallback_to_delayed_is_reported():
    assert reference(snap(99.0, 101.0, data_type=3))[1] == 3


def test_configured_delayed_wins_even_if_ticker_says_live():
    assert reference(snap(99.0, 101.0, data_type=1), configured=3)[1] == 3


def test_quote_error_gives_no_reference():
    assert reference(RuntimeError("no data"))[0] is None
