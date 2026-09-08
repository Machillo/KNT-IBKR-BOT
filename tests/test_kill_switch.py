from types import SimpleNamespace

import asyncio

from config.config import RiskConfig
from risk.kill_switch import KillSwitch


class FakeTrade:
    def __init__(self, account=""):
        self.order = SimpleNamespace(account=account)
    def isDone(self):
        return False


class FakeIB:
    def __init__(self):
        self.cancelled = []
        self.placed = []
        self._trades = [FakeTrade("DU_TEST")]
        self._positions = [SimpleNamespace(
            account="DU_TEST",
            position=3,
            contract=SimpleNamespace(localSymbol="ABC", symbol="ABC"),
        )]
    def openTrades(self): return self._trades
    def positions(self): return self._positions
    def cancelOrder(self, order): self.cancelled.append(order)
    def placeOrder(self, contract, order):
        self.placed.append((contract, order))
        return SimpleNamespace(isDone=lambda: True)


def test_dry_run_never_touches_broker_orders():
    ib = FakeIB()
    settings = RiskConfig(kill_switch_enabled=True, kill_switch_dry_run=True)
    result = asyncio.run(KillSwitch(ib, settings, "DU_TEST").execute("test trigger"))
    assert result.triggered
    assert result.dry_run
    assert ib.cancelled == []
    assert ib.placed == []
