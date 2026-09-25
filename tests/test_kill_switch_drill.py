import asyncio
from types import SimpleNamespace

from config.config import IBKRConfig
from run_kill_switch_paper_drill import DRILL_ACK, run_drill

ACCOUNT = "DU0000001"
SETTINGS = IBKRConfig(port=7497, allow_live_trading=False, account=None)


class FakeIB:
    def __init__(self, accounts=(ACCOUNT,), qty=1, sec_type="STK"):
        self.accounts = list(accounts)
        self.client = SimpleNamespace(port=7497, clientId=1)
        self.placed, self.cancelled = [], []
        self._positions = [SimpleNamespace(account=ACCOUNT, position=qty,
                                           contract=SimpleNamespace(symbol="ABC", localSymbol="ABC", secType=sec_type,
                                                                   conId=1))]
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


def test_kill_switch_fails_closed_when_the_every_client_view_is_unavailable():
    ib = GtcIB([("AAA", 11, 1, "STK")])

    async def broken():
        raise TimeoutError("no answer")
    ib.reqAllOpenOrdersAsync = broken
    result = _armed(ib)
    assert ib.placed == [] and result.flat_confirmed is False


def test_kill_switch_never_cancels_another_clients_order_ids():
    # ib_async may cache other clients' orders after reqAllOpenOrders; their orderIds live in
    # another namespace, so cancelling one could hit OUR order with the same number.
    ib = GtcIB([("AAA", 11, 1, "STK")], own=(11,))
    ib.client = SimpleNamespace(port=7497, clientId=901)
    foreign = GtcIB._order(11)
    foreign.order.clientId = 7
    ib._trades[0].order.clientId = 901
    ib._trades.append(foreign)
    _armed(ib)
    assert all(getattr(o, "clientId", 901) == 901 for o in ib.cancelled)


def test_kill_switch_may_retry_while_the_account_is_not_flat():
    import asyncio as aio
    from config.config import RiskConfig
    from core.paper_guard import build_paper_guard
    from risk.kill_switch import KillSwitch

    ib = GtcIB([("AAA", 11, 1, "STK")], other=(11,))
    ks = KillSwitch(ib, RiskConfig(kill_switch_enabled=True, kill_switch_dry_run=False), ACCOUNT,
                    guard=build_paper_guard(ib, SETTINGS))
    assert aio.run(ks.execute("daily loss")).flat_confirmed is False
    ib.other_clients = []                                   # the human cancelled the foreign stop
    assert aio.run(ks.execute("daily loss")).flat_confirmed is True and len(ib.placed) == 1


def _switch(ib):
    from config.config import RiskConfig
    from core.paper_guard import build_paper_guard
    from risk.kill_switch import KillSwitch

    return KillSwitch(ib, RiskConfig(kill_switch_enabled=True, kill_switch_dry_run=False), ACCOUNT,
                      guard=build_paper_guard(ib, SETTINGS))


def test_kill_switch_keeps_own_stop_when_the_every_client_view_is_unavailable():
    # Without the every-client view it cannot flatten; cancelling the stop first would leave the
    # position unprotected. Entries without a position may still be cancelled.
    import asyncio as aio

    ib = GtcIB([("AAA", 11, 1, "STK")], own=(11, 12))

    async def blind():
        raise TimeoutError("no answer")
    ib.reqAllOpenOrdersAsync = blind
    result = aio.run(_switch(ib).execute("daily loss"))
    assert [o.orderId for o in ib.cancelled] == [12]
    assert ib.placed == [] and result.flat_confirmed is False


def test_kill_switch_keeps_own_stop_when_another_client_works_the_same_contract():
    import asyncio as aio

    ib = GtcIB([("AAA", 11, 1, "STK")], own=(11,))
    ib.client = SimpleNamespace(port=7497, clientId=7)
    ib._trades[0].order.clientId = 7
    foreign = GtcIB._order(11)
    foreign.order.clientId = 99
    ib.other_clients = [foreign]
    result = aio.run(_switch(ib).execute("daily loss"))
    assert ib.cancelled == [] and ib.placed == [] and result.flat_confirmed is False


def test_kill_switch_rerun_never_cancels_or_duplicates_its_own_pending_liquidation():
    import asyncio as aio

    class SlowFill(GtcIB):
        def placeOrder(self, contract, order):
            self.placed.append((contract, order))
            order.orderId = 500
            trade = SimpleNamespace(order=order, contract=contract, done=False)
            trade.isDone = lambda: False
            self._trades.append(trade)                      # working, position not yet gone
            return trade

    ib = SlowFill([("AAA", 11, 1, "STK")], own=(11,))
    switch = _switch(ib)
    first = aio.run(switch.execute("daily loss"))
    assert len(ib.placed) == 1 and first.flat_confirmed is False
    second = aio.run(switch.execute("daily loss"))
    assert [o.orderId for o in ib.cancelled] == [11]        # the liquidation order is never cancelled
    assert len(ib.placed) == 1 and second.flat_confirmed is False   # and never duplicated


def test_kill_switch_classifies_a_position_that_opened_while_it_waited_for_the_view():
    # An own entry fills during the every-client request: its fresh stop must not be cancelled
    # unless the position is flattened.
    import asyncio as aio

    ib = GtcIB([], own=(31,))
    ib.client = SimpleNamespace(port=7497, clientId=7)
    ib._trades[0].order.clientId = 7
    original = ib.reqAllOpenOrdersAsync

    async def slow_view():
        ib._positions = [SimpleNamespace(account=ACCOUNT, position=1.5, contract=SimpleNamespace(
            symbol="NEW", localSymbol="NEW", secType="STK", conId=31))]        # fractional: manual
        return await original()
    ib.reqAllOpenOrdersAsync = slow_view
    result = aio.run(_switch(ib).execute("daily loss"))
    assert ib.cancelled == [] and ib.placed == [] and result.flat_confirmed is False


def test_kill_switch_without_its_own_client_id_treats_the_view_as_unusable():
    import asyncio as aio

    ib = GtcIB([("AAA", 11, 1, "STK")], own=(11,))
    ib.client = SimpleNamespace(port=7497)                                   # clientId unknown
    result = aio.run(_switch(ib).execute("daily loss"))
    assert ib.cancelled == [] and ib.placed == [] and result.flat_confirmed is False
