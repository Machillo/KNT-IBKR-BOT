import asyncio
from types import SimpleNamespace

import pytest

from config.config import IBKRConfig, RiskConfig
from core.order_manager import OrderManager
from core.paper_guard import (
    PaperGuardError, PaperOrderGuard, PaperVerification, build_paper_guard, is_paper_account_id,
    mask_account, verify_paper_account,
)
from risk.kill_switch import KillSwitch

ACCOUNT = "DU0000001"


class FakeIB:
    def __init__(self, accounts=(ACCOUNT,), port=7497, connected=True):
        self.accounts = list(accounts)
        self.client = SimpleNamespace(port=port)
        self.connected = connected
        self.placed = []
        self.cancelled = []
        self._trades = []
        self._positions = []

    def isConnected(self):
        return self.connected

    def managedAccounts(self):
        return list(self.accounts)

    def placeOrder(self, contract, order):
        self.placed.append((contract, order))
        return SimpleNamespace(
            order=order, orderStatus=SimpleNamespace(status="Submitted"),
            statusEvent=_Hook(), fillEvent=_Hook(), isDone=lambda: True,
        )

    def cancelOrder(self, order):
        self.cancelled.append(order)

    def openTrades(self):
        return list(self._trades)

    def positions(self):
        return list(self._positions)


class _Hook:
    def __iadd__(self, cb):
        return self


def settings(**kw):
    values = dict(port=7497, allow_live_trading=False, account=None)
    values.update(kw)
    return IBKRConfig(**values)


@pytest.mark.parametrize("account,expected", [
    ("DU0000001", True), ("DF0000001", True), ("U0000001", False), ("F0000001", False),
    ("DUX", False), ("", False), (None, False), ("du0000001", False),
])
def test_paper_account_prefix(account, expected):
    assert is_paper_account_id(account) is expected


def test_verified_single_paper_account():
    v = verify_paper_account(FakeIB(), settings())
    assert v.verified and v.account == ACCOUNT and v.reason == "verified_paper"


@pytest.mark.parametrize("ib,cfg,reason", [
    (FakeIB(), settings(allow_live_trading=True), "live_trading_allowed_in_config"),
    (FakeIB(port=7496), settings(port=7496), "configured_port_not_paper"),
    (FakeIB(connected=False), settings(), "not_connected"),
    (FakeIB(port=None), settings(), "connected_port_unknown"),
    (FakeIB(port=7496), settings(), "connected_port_mismatch"),
    (FakeIB(accounts=()), settings(), "no_managed_accounts"),
    (FakeIB(accounts=("U0000001",)), settings(), "non_paper_account_in_session"),
    (FakeIB(accounts=(ACCOUNT, "U0000001")), settings(), "non_paper_account_in_session"),
    (FakeIB(accounts=(ACCOUNT, "DU0000002")), settings(), "ambiguous_account_selection"),
    (FakeIB(), settings(account="DU0000009"), "account_not_managed_by_session"),
])
def test_verification_refusals(ib, cfg, reason):
    v = verify_paper_account(ib, cfg)
    assert v.verified is False
    assert v.reason == reason


def test_configured_and_requested_accounts_must_agree():
    ib = FakeIB(accounts=(ACCOUNT, "DU0000002"))
    v = verify_paper_account(ib, settings(account=ACCOUNT), "DU0000002")
    assert (v.verified, v.reason) == (False, "account_differs_from_configured")
    assert verify_paper_account(ib, settings(account=ACCOUNT)).verified


def test_mask_account_never_returns_full_id():
    assert ACCOUNT not in mask_account(ACCOUNT)
    assert mask_account(None) == "<none>"


def test_order_manager_without_guard_never_transmits():
    ib = FakeIB()
    orders = OrderManager(ib, ACCOUNT)
    with pytest.raises(PaperGuardError):
        orders.limit(SimpleNamespace(symbol="X"), "BUY", 1, 10.0)
    with pytest.raises(PaperGuardError):
        orders.market(SimpleNamespace(symbol="X"), "BUY", 1)
    assert ib.placed == []


def test_order_manager_with_verified_guard_transmits_with_account():
    ib = FakeIB()
    orders = OrderManager(ib, guard=build_paper_guard(ib, settings()))
    orders.limit(SimpleNamespace(symbol="X", localSymbol="X"), "BUY", 1, 10.0)
    assert len(ib.placed) == 1
    assert ib.placed[0][1].account == ACCOUNT


def test_order_manager_refuses_when_session_becomes_live():
    ib = FakeIB()
    orders = OrderManager(ib, guard=build_paper_guard(ib, settings()))
    ib.accounts = ["U0000001"]
    with pytest.raises(PaperGuardError):
        orders.limit(SimpleNamespace(symbol="X"), "BUY", 1, 10.0)
    assert ib.placed == []


def test_order_manager_refuses_unverified_guard():
    ib = FakeIB(accounts=("U0000001",))
    orders = OrderManager(ib, guard=build_paper_guard(ib, settings()))
    with pytest.raises(PaperGuardError):
        orders.market(SimpleNamespace(symbol="X"), "BUY", 1)
    assert ib.placed == []


def test_order_manager_rejects_account_mismatch_with_guard():
    ib = FakeIB()
    with pytest.raises(PaperGuardError):
        OrderManager(ib, "DU0000002", guard=build_paper_guard(ib, settings()))


def test_guard_rejects_order_for_other_account():
    ib = FakeIB()
    guard = build_paper_guard(ib, settings())
    with pytest.raises(PaperGuardError):
        guard.assert_can_transmit(SimpleNamespace(account="DU0000002"))


def test_forged_verification_is_still_reverified():
    ib = FakeIB(accounts=("U0000001",))
    forged = PaperVerification(True, "U0000001", "verified_paper", 7497, 7497)
    with pytest.raises(PaperGuardError):
        PaperOrderGuard(ib, settings(), forged).assert_can_transmit(SimpleNamespace(account="U0000001"))


def _armed_kill_switch(ib, guard):
    trade = SimpleNamespace(order=SimpleNamespace(account=ACCOUNT), isDone=lambda: False)
    ib._trades = [trade]
    ib._positions = [SimpleNamespace(account=ACCOUNT, position=3,
                                     contract=SimpleNamespace(localSymbol="ABC", symbol="ABC"))]
    cfg = RiskConfig(kill_switch_enabled=True, kill_switch_dry_run=False)
    return KillSwitch(ib, cfg, ACCOUNT, guard=guard)


def test_armed_kill_switch_without_guard_touches_nothing():
    ib = FakeIB()
    result = asyncio.run(_armed_kill_switch(ib, None).execute("test"))
    assert result.triggered and not result.flat_confirmed
    assert ib.cancelled == [] and ib.placed == []


def test_armed_kill_switch_on_live_session_touches_nothing():
    ib = FakeIB()
    guard = build_paper_guard(ib, settings())
    ks = _armed_kill_switch(ib, guard)
    ib.accounts = ["U0000001"]
    asyncio.run(ks.execute("test"))
    assert ib.cancelled == [] and ib.placed == []
