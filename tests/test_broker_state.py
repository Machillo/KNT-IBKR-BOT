from types import SimpleNamespace

from core.broker_state import BrokerStateService


class FakeTrade:
    def __init__(self, account=""):
        self.order = SimpleNamespace(account=account)
    def isDone(self):
        return False


class FakeIB:
    def __init__(self):
        self._positions = []
        self._trades = []
    def positions(self):
        return self._positions
    def openTrades(self):
        return self._trades


def pos(account, symbol, qty):
    return SimpleNamespace(
        account=account,
        position=qty,
        contract=SimpleNamespace(localSymbol=symbol, symbol=symbol, conId=1),
    )


def test_broker_state_is_account_scoped():
    ib = FakeIB()
    ib._positions = [pos("DU_A", "AAA", 1), pos("DU_B", "BBB", 2)]
    ib._trades = [FakeTrade("DU_A"), FakeTrade("DU_B")]
    state = BrokerStateService(ib).snapshot("DU_A")
    assert state.position_count == 1
    assert state.open_order_count == 1
    assert state.nonzero_positions == (("AAA", 1.0),)


def test_empty_account_state_is_flat():
    state = BrokerStateService(FakeIB()).snapshot("DU_A")
    assert state.is_flat
