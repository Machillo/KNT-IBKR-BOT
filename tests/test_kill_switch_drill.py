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


def test_kill_switch_never_market_flattens_non_stock_positions():
    import asyncio as aio
    from config.config import RiskConfig
    from core.paper_guard import build_paper_guard
    from risk.kill_switch import KillSwitch

    for sec_type in ("FUT", "OPT", "CASH"):
        ib = FakeIB(qty=1, sec_type=sec_type)
        ks = KillSwitch(ib, RiskConfig(kill_switch_enabled=True, kill_switch_dry_run=False), ACCOUNT,
                        guard=build_paper_guard(ib, SETTINGS))
        result = aio.run(ks.execute("daily loss"))
        assert ib.placed == [] and result.flat_confirmed is False, sec_type
    stock = FakeIB(qty=1)
    KillSwitch(stock, RiskConfig(kill_switch_enabled=True, kill_switch_dry_run=False), ACCOUNT,
               guard=build_paper_guard(stock, SETTINGS))
    result = aio.run(KillSwitch(stock, RiskConfig(kill_switch_enabled=True, kill_switch_dry_run=False), ACCOUNT,
                                guard=build_paper_guard(stock, SETTINGS)).execute("daily loss"))
    assert len(stock.placed) == 1                                         # stocks are still flattened


class GtcIB(FakeIB):
    """Positions and working orders carry conIds; cancels mark this client's orders done."""

    def __init__(self, positions, own=(), other=()):
        super().__init__()
        self._positions = [SimpleNamespace(account=ACCOUNT, position=q, contract=SimpleNamespace(
            symbol=sym, localSymbol=sym, secType=st, conId=cid)) for sym, cid, q, st in positions]
        self._trades = [self._order(cid) for cid in own]
        self.other_clients = [self._order(cid) for cid in other]

    @staticmethod
    def _order(cid):
        trade = SimpleNamespace(order=SimpleNamespace(account=ACCOUNT, orderId=cid), contract=SimpleNamespace(conId=cid),
                                done=False)
        trade.isDone = lambda t=trade: t.done
        return trade

    def cancelOrder(self, order):
        self.cancelled.append(order)
        for t in self._trades:
            if t.order is order:
                t.done = True

    def placeOrder(self, contract, order):
        self.placed.append((contract, order))
        self._positions = [p for p in self._positions if p.contract is not contract]
        return SimpleNamespace(isDone=lambda: True)


def _armed(ib):
    import asyncio as aio
    from config.config import RiskConfig
    from core.paper_guard import build_paper_guard
    from risk.kill_switch import KillSwitch

    return aio.run(KillSwitch(ib, RiskConfig(kill_switch_enabled=True, kill_switch_dry_run=False), ACCOUNT,
                              guard=build_paper_guard(ib, SETTINGS)).execute("daily loss"))


def test_kill_switch_cancels_own_protection_then_flattens_the_stock():
    ib = GtcIB([("AAA", 11, 1, "STK")], own=(11,))
    result = _armed(ib)
    assert len(ib.cancelled) == 1 and len(ib.placed) == 1 and result.flat_confirmed


def test_kill_switch_never_flattens_under_another_clients_live_gtc_stop():
    # The stop belongs to another clientId: this session cannot cancel it. A market flatten now
    # would leave the stop live to fire later and REVERSE the position.
    ib = GtcIB([("AAA", 11, 1, "STK")], other=(11,))
    result = _armed(ib)
    assert ib.placed == [] and result.flat_confirmed is False


def test_kill_switch_keeps_the_protection_of_positions_it_leaves_to_a_human():
    ib = GtcIB([("ESZ6", 21, 1, "FUT"), ("AAA", 11, 1, "STK")], own=(21, 11))
    result = _armed(ib)
    cancelled_ids = [o.orderId for o in ib.cancelled]
    assert cancelled_ids == [11]                                  # the FUT's stop is left in place
    assert [c.symbol for c, _ in ib.placed] == ["AAA"] and result.flat_confirmed is False


def test_kill_switch_skips_fractional_stock_positions():
    ib = GtcIB([("AAA", 11, 0.5, "STK")])
    result = _armed(ib)
    assert ib.placed == [] and result.flat_confirmed is False
