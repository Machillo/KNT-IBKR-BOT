import asyncio
from types import SimpleNamespace

from config.config import IBKRConfig
from run_kill_switch_paper_drill import DRILL_ACK, run_drill

ACCOUNT = "DU0000001"
SETTINGS = IBKRConfig(port=7497, allow_live_trading=False, account=None)


class FakeIB:
    def __init__(self, accounts=(ACCOUNT,), qty=1, sec_type="STK"):
        self.accounts = list(accounts)
        self.client = SimpleNamespace(port=7497)
        self.placed, self.cancelled = [], []
        self._positions = [SimpleNamespace(account=ACCOUNT, position=qty,
                                           contract=SimpleNamespace(symbol="ABC", localSymbol="ABC", secType=sec_type))]
        self._trades = []

    def isConnected(self):
        return True

    def managedAccounts(self):
        return list(self.accounts)

    def positions(self):
        return list(self._positions)

    def openTrades(self):
        return list(self._trades)

    other_clients: list = []

    async def reqAllOpenOrdersAsync(self):
        return list(self._trades) + list(self.other_clients)

    def cancelOrder(self, order):
        self.cancelled.append(order)

    def placeOrder(self, contract, order):
        self.placed.append(order)
        self._positions = []
        return SimpleNamespace(isDone=lambda: True)


def drill(ib, **kw):
    return asyncio.run(run_drill(ib, SETTINGS, **kw))


def test_default_dry_run_sends_nothing():
    ib = FakeIB()
    out = drill(ib, armed=False, ack="")
    assert out.ran and not out.armed and ib.placed == [] and ib.cancelled == []


def test_armed_drill_needs_literal_ack():
    ib = FakeIB()
    assert drill(ib, armed=True, ack="yes").reason == "missing_drill_ack"
    assert ib.placed == []


def test_armed_drill_refuses_live_or_unverified_sessions():
    ib = FakeIB(accounts=("U0000001",))
    out = drill(ib, armed=True, ack=DRILL_ACK)
    assert not out.ran and out.reason.startswith("paper_unverified") and ib.placed == []


def test_armed_drill_refuses_positions_beyond_drill_limits():
    for ib in (FakeIB(qty=100), FakeIB(sec_type="OPT")):
        assert drill(ib, armed=True, ack=DRILL_ACK).reason == "positions_exceed_drill_limits"
        assert ib.placed == []


def test_armed_drill_flattens_one_share_through_the_guard():
    ib = FakeIB()
    out = drill(ib, armed=True, ack=DRILL_ACK)
    assert out.ran and out.armed and out.liquidation_orders == 1 and out.flat_confirmed
    assert ib.placed[0].account == ACCOUNT and ib.placed[0].totalQuantity == 1


def test_drill_refuses_open_orders_and_caps_quantity_and_persisted_ack():
    ib = FakeIB()
    ib._trades = [SimpleNamespace(isDone=lambda: False, order=SimpleNamespace(account=ACCOUNT))]
    assert drill(ib, armed=True, ack=DRILL_ACK).reason == "open_orders_present"
    assert drill(FakeIB(), armed=True, ack=DRILL_ACK, max_qty=1e9).reason == "max_qty_out_of_range"
    assert drill(FakeIB(), armed=True, ack=DRILL_ACK, persisted_ack=DRILL_ACK).reason == "ack_must_not_be_persisted_in_env"
    assert ib.placed == []


def test_kill_switch_liquidation_limit_skips_oversized_positions():
    import asyncio as aio
    from config.config import RiskConfig
    from core.paper_guard import build_paper_guard
    from risk.kill_switch import KillSwitch

    ib = FakeIB(qty=50)
    ks = KillSwitch(ib, RiskConfig(kill_switch_enabled=True, kill_switch_dry_run=False), ACCOUNT,
                    guard=build_paper_guard(ib, SETTINGS), liquidation_qty_limit=1)
    result = aio.run(ks.execute("late fill"))
    assert ib.placed == [] and result.flat_confirmed is False


def test_drill_sees_other_clients_working_orders_and_fails_closed():
    ib = FakeIB()
    ib.other_clients = [SimpleNamespace(isDone=lambda: False, order=SimpleNamespace(account=ACCOUNT))]
    assert drill(ib, armed=True, ack=DRILL_ACK).reason == "open_orders_present_other_clients"
    blind = FakeIB()

    async def boom():
        raise TimeoutError("no answer")
    blind.reqAllOpenOrdersAsync = boom
    assert drill(blind, armed=True, ack=DRILL_ACK).reason == "open_orders_unverifiable"
    assert ib.placed == [] and blind.placed == [] and ib.cancelled == []
