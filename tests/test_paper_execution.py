import asyncio
from types import SimpleNamespace

import pytest

from config.config import IBKRConfig, RiskConfig
from core.paper_guard import build_paper_guard
from execution.paper import (
    AUTONOMOUS_PAPER_ACK, PaperExecutionEngine, PaperExecutionRequest, TradeJournalStore,
    autonomous_paper_armed,
)
from risk.risk_manager import RiskManager

ACCOUNT = "DU0000001"
PAPER_SETTINGS = IBKRConfig(port=7497, allow_live_trading=False, account=None)


class EventHook:
    def __init__(self):
        self.callbacks = []

    def __iadd__(self, callback):
        self.callbacks.append(callback)
        return self


class FakeTrade:
    def __init__(self, symbol="AAPL", account=ACCOUNT, order=None, status="Submitted", log=()):
        self.contract = SimpleNamespace(localSymbol=symbol, symbol=symbol)
        self.order = order or SimpleNamespace(account=account, orderId=1, action="BUY")
        self.orderStatus = SimpleNamespace(status=status, filled=0, remaining=1, avgFillPrice=0)
        self.statusEvent = EventHook()
        self.fillEvent = EventHook()
        self.log = list(log)

    def isDone(self):
        return self.orderStatus.status in {"Cancelled", "ApiCancelled", "Inactive", "Filled"}


class FakeBracketOrder:
    def __init__(self, parent, take_profit, stop_loss):
        self.parent = parent
        self.takeProfit = take_profit
        self.stopLoss = stop_loss

    def __iter__(self):
        return iter((self.parent, self.takeProfit, self.stopLoss))


class FakeIB:
    def __init__(self, *, positions=(), trades=(), reject_index=None, accounts=(ACCOUNT,), port=7497,
                 connected=True):
        self.accounts = list(accounts)
        self.client = SimpleNamespace(port=port)
        self.connected = connected
        self._positions = positions
        self._trades = trades
        self.submitted = []
        self.bracket_args = None
        self.reject_index = reject_index

    net_liquidation = 100_000.0

    def isConnected(self):
        return self.connected

    def accountValues(self, account=""):
        if self.net_liquidation is None:
            return []
        return [SimpleNamespace(account=ACCOUNT, tag="NetLiquidation", currency="BASE",
                                value=str(self.net_liquidation))]

    def managedAccounts(self):
        return list(self.accounts)

    def portfolio(self, account):
        return list(self._positions)

    def openTrades(self):
        return list(self._trades)

    def bracketOrder(self, action, quantity, entry, target, stop, **kwargs):
        self.bracket_args = (action, quantity, entry, target, stop, kwargs)
        parent = SimpleNamespace(orderId=101, account="", algoStrategy="", algoParams=[], transmit=False, action=action)
        tp = SimpleNamespace(orderId=102, account="", transmit=False, action="SELL" if action == "BUY" else "BUY")
        sl = SimpleNamespace(orderId=103, account="", transmit=True, action="SELL" if action == "BUY" else "BUY")
        return FakeBracketOrder(parent, tp, sl)

    def placeOrder(self, contract, order):
        index = len(self.submitted)
        status = "Cancelled" if self.reject_index == index else "Submitted"
        log = (SimpleNamespace(errorCode=110),) if self.reject_index == index else ()
        trade = FakeTrade(getattr(contract, "symbol", "AAPL"), getattr(order, "account", ""), order=order, status=status, log=log)
        self.submitted.append((contract, order, trade))
        return trade

    def cancelOrder(self, order):
        return None


class FakeSessionPolicy:
    def __init__(self, market_open=True):
        self.market_open = market_open

    def state(self):
        return SimpleNamespace(market_open=self.market_open)


def request(**overrides):
    values = dict(
        symbol="AAPL", strategy="momentum_v1", side="LONG", quantity=10,
        entry_price=100, stop_price=95, target_price=110, regime="TRENDING",
        account_equity=100_000.0, reference_price=100.0, market_data_type=1,
    )
    values.update(overrides)
    return PaperExecutionRequest(**values)


def engine(tmp_path, ib=None, **kwargs):
    ib = ib or FakeIB()
    kwargs.setdefault("paper_guard", build_paper_guard(ib, PAPER_SETTINGS, ACCOUNT))
    kwargs.setdefault("risk_manager", RiskManager(RiskConfig()))
    kwargs.setdefault("session_policy", FakeSessionPolicy(True))
    return PaperExecutionEngine(
        ib,
        account=ACCOUNT,
        journal=TradeJournalStore(tmp_path / "j.db"),
        acceptance_delay_seconds=0,
        **kwargs,
    )


def stock(symbol="AAPL"):
    return SimpleNamespace(symbol=symbol, localSymbol=symbol, secType="STK")


def run(coro):
    return asyncio.run(coro)


def test_paper_execution_is_disabled_by_default(tmp_path):
    result = run(engine(tmp_path, enabled=False).submit(stock(), request()))
    assert result.submitted is False
    assert result.reason == "autonomous_paper_disabled"


def test_paper_execution_requires_a_paper_guard(tmp_path):
    ib = FakeIB()
    result = run(engine(tmp_path, ib, enabled=True, paper_guard=None).submit(stock(), request()))
    assert (result.submitted, result.reason) == (False, "paper_guard_missing")
    assert ib.submitted == []


@pytest.mark.parametrize(
    "ib_kwargs",
    [
        {"accounts": ("U0000001",)},          # live-style account behind a "paper" port
        {"accounts": (ACCOUNT, "U0000002")},  # mixed session lists a non-paper account
        {"port": 7496},                       # the socket really is a live port
        {"port": 4002},                       # socket differs from configured port
        {"accounts": ()},                     # no managed accounts
        {"connected": False},
    ],
)
def test_paper_execution_refuses_unverifiable_sessions(tmp_path, ib_kwargs):
    ib = FakeIB(**ib_kwargs)
    result = run(engine(tmp_path, ib, enabled=True).submit(stock(), request()))
    assert result.submitted is False
    assert result.reason in {"paper_account_unverified", "paper_guard_account_mismatch"}
    assert ib.submitted == []


def test_paper_execution_reverifies_session_before_transmitting(tmp_path):
    ib = FakeIB()
    e = engine(tmp_path, ib, enabled=True)
    ib.accounts = ["U0000001"]  # session changed after startup verification (e.g. reconnect)
    result = run(e.submit(stock(), request()))
    assert (result.submitted, result.reason) == (False, "paper_reverification_failed")
    assert ib.submitted == []


def test_paper_execution_blocks_when_disconnected_after_verification(tmp_path):
    ib = FakeIB()
    e = engine(tmp_path, ib, enabled=True)
    ib.connected = False
    result = run(e.submit(stock(), request()))
    assert result.submitted is False
    assert ib.submitted == []


def test_paper_execution_requires_risk_manager(tmp_path):
    ib = FakeIB()
    result = run(engine(tmp_path, ib, enabled=True, risk_manager=None).submit(stock(), request()))
    assert (result.submitted, result.reason) == (False, "risk_manager_required")
    assert ib.submitted == []


def test_paper_execution_reapplies_hard_risk_limits(tmp_path):
    ib = FakeIB()
    # 10 shares x 5.00 stop distance = 50 at risk > 10% of 400 equity.
    result = run(engine(tmp_path, ib, enabled=True).submit(stock(), request(account_equity=400.0)))
    assert result.submitted is False
    assert result.reason.startswith("hard_risk:")
    assert ib.submitted == []


def test_paper_execution_rejects_missing_equity(tmp_path):
    ib = FakeIB()
    result = run(engine(tmp_path, ib, enabled=True).submit(stock(), request(account_equity=0.0)))
    assert result.reason == "hard_risk:invalid_inputs"
    assert ib.submitted == []


@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({"market_data_type": 3}, "reference_not_live_market_data"),
        ({"market_data_type": None}, "reference_not_live_market_data"),
        ({"reference_price": None}, "fresh_reference_price_missing"),
        ({"reference_price": 90.0}, "entry_far_from_fresh_reference"),
    ],
)
def test_paper_execution_requires_fresh_live_reference(tmp_path, overrides, reason):
    ib = FakeIB()
    result = run(engine(tmp_path, ib, enabled=True).submit(stock(), request(**overrides)))
    assert (result.submitted, result.reason) == (False, reason)
    assert ib.submitted == []


def test_paper_execution_rejects_shorts_until_shortability_is_modelled(tmp_path):
    ib = FakeIB()
    result = run(engine(tmp_path, ib, enabled=True).submit(
        stock(), request(side="SHORT", stop_price=105, target_price=95)))
    assert (result.submitted, result.reason) == (False, "short_entries_disabled")
    assert ib.submitted == []


def test_paper_execution_rejects_fractional_stock_quantity(tmp_path):
    result = run(engine(tmp_path, enabled=True).submit(stock(), request(quantity=1.5)))
    assert result.reason == "unsupported_instrument_or_quantity"


def test_paper_execution_enforces_daily_entry_limit(tmp_path):
    ib = FakeIB()
    e = engine(tmp_path, ib, enabled=True, max_entries_per_day=1)
    first = run(e.submit(stock("AAPL"), request(symbol="AAPL")))
    ib.submitted.clear()
    second = run(e.submit(stock("MSFT"), request(symbol="MSFT")))
    assert first.submitted is True
    assert (second.submitted, second.reason) == (False, "daily_entry_limit_reached")
    assert ib.submitted == []


def test_paper_execution_detects_duplicate_by_con_id(tmp_path):
    position = SimpleNamespace(contract=SimpleNamespace(localSymbol="OTHER", symbol="OTHER", conId=42), position=5)
    result = run(engine(tmp_path, FakeIB(positions=(position,)), enabled=True).submit(
        stock(), request(con_id=42)))
    assert result.reason == "duplicate_symbol_exposure"


def test_paper_execution_without_session_policy_fails_closed(tmp_path):
    ib = FakeIB()
    result = run(engine(tmp_path, ib, enabled=True, session_policy=None).submit(stock(), request()))
    assert result.reason == "market_session_closed"
    assert ib.submitted == []


def test_autonomous_paper_requires_flag_and_literal_ack():
    assert autonomous_paper_armed(False, AUTONOMOUS_PAPER_ACK) is False
    assert autonomous_paper_armed(True, "") is False
    assert autonomous_paper_armed(True, "yes") is False
    assert autonomous_paper_armed(True, AUTONOMOUS_PAPER_ACK) is True


def test_paper_execution_blocks_when_regular_stock_session_is_closed(tmp_path):
    ib = FakeIB()
    result = run(engine(
        tmp_path,
        ib,
        enabled=True,
        session_policy=FakeSessionPolicy(False),
    ).submit(stock(), request()))
    assert result.submitted is False
    assert result.reason == "market_session_closed"
    assert ib.submitted == []


def test_paper_execution_allows_open_regular_stock_session(tmp_path):
    ib = FakeIB()
    result = run(engine(
        tmp_path,
        ib,
        enabled=True,
        session_policy=FakeSessionPolicy(True),
    ).submit(stock(), request()))
    assert result.submitted is True
    assert len(ib.submitted) == 3


def test_paper_execution_rejects_locked_risk_manager(tmp_path):
    result = run(engine(tmp_path, enabled=True, risk_manager=SimpleNamespace(trading_locked=True)).submit(stock(), request()))
    assert result.submitted is False
    assert result.reason == "risk_manager_locked"


def test_paper_execution_rejects_duplicate_position(tmp_path):
    position = SimpleNamespace(contract=SimpleNamespace(localSymbol="AAPL", symbol="AAPL"), position=5)
    result = run(engine(tmp_path, FakeIB(positions=(position,)), enabled=True).submit(stock(), request()))
    assert result.submitted is False
    assert result.reason == "duplicate_symbol_exposure"


def test_paper_execution_rejects_duplicate_open_order(tmp_path):
    result = run(engine(tmp_path, FakeIB(trades=(FakeTrade("AAPL", ACCOUNT),)), enabled=True).submit(stock(), request()))
    assert result.submitted is False
    assert result.reason == "duplicate_symbol_exposure"


def test_paper_execution_rejects_invalid_price_geometry(tmp_path):
    result = run(engine(tmp_path, enabled=True).submit(stock(), request(stop_price=101, target_price=110)))
    assert result.submitted is False
    assert result.reason == "invalid_price_geometry"


def test_paper_execution_normalizes_stock_prices_and_confirms_three_legs(tmp_path):
    ib = FakeIB()
    result = run(engine(tmp_path, ib, enabled=True).submit(
        stock("INTC"),
        request(symbol="INTC", entry_price=105.98, stop_price=102.8675, target_price=111.16750000000002,
                reference_price=106.0),
    ))
    assert result.submitted is True
    assert result.reason == "bracket_confirmed"
    assert result.parent_order_id == 101
    assert ib.bracket_args[:5] == ("BUY", 10, 105.98, 111.17, 102.87)
    assert len(ib.submitted) == 3
    parent, tp, sl = [order for _, order, _ in ib.submitted]
    assert parent.account == ACCOUNT
    assert tp.account == ACCOUNT
    assert sl.account == ACCOUNT
    assert parent.algoStrategy == "Adaptive"


def test_paper_execution_short_geometry_and_mapping(tmp_path):
    ib = FakeIB()
    result = run(engine(tmp_path, ib, enabled=True, allow_short=True).submit(
        stock("AAPL"), request(side="SHORT", entry_price=100.001, stop_price=105.004, target_price=95.002),
    ))
    assert result.submitted is True
    assert ib.bracket_args[:5] == ("SELL", 10, 100.0, 95.0, 105.0)


def test_paper_execution_fails_when_child_leg_is_rejected(tmp_path):
    ib = FakeIB(reject_index=1)
    result = run(engine(tmp_path, ib, enabled=True).submit(stock(), request()))
    assert result.submitted is False
    assert result.reason in {"order_status_cancelled", "broker_error_110"}


def test_paper_execution_refuses_without_broker_equity(tmp_path):
    ib = FakeIB()
    ib.net_liquidation = None
    result = run(engine(tmp_path, ib, enabled=True).submit(stock(), request()))
    assert (result.submitted, result.reason) == (False, "broker_equity_unavailable")
    assert ib.submitted == []


def test_paper_execution_uses_lower_of_caller_and_broker_equity(tmp_path):
    ib = FakeIB()
    ib.net_liquidation = 400.0  # caller claims 100k; broker says 400 -> 50 at risk > 40 cap
    result = run(engine(tmp_path, ib, enabled=True).submit(stock(), request()))
    assert result.reason.startswith("hard_risk:")
    assert ib.submitted == []


def test_persisted_ack_never_arms_autonomous_paper():
    assert autonomous_paper_armed(True, AUTONOMOUS_PAPER_ACK, persisted_ack=AUTONOMOUS_PAPER_ACK) is False


class ExplodingFlattenIB(FakeIB):
    """Parent filled, child rejected, then the session dies while flattening."""

    def placeOrder(self, contract, order):
        if len(self.submitted) >= 3:
            raise ConnectionError("socket closed")
        trade = super().placeOrder(contract, order)
        if len(self.submitted) == 1:
            trade.orderStatus.filled = 10
        return trade


def test_unwind_failure_records_and_locks_trading(tmp_path):
    ib = ExplodingFlattenIB(reject_index=1)
    risk = RiskManager(RiskConfig())
    result = run(engine(tmp_path, ib, enabled=True, risk_manager=risk).submit(stock(), request()))
    assert result.submitted is False
    assert result.reason.startswith("unwind_failed_manual_intervention")
    assert risk.trading_locked is True


class ExplodingTransmitIB(FakeIB):
    def placeOrder(self, contract, order):
        raise ConnectionError("socket closed")


def test_transmission_error_counts_toward_cap_and_locks(tmp_path):
    ib = ExplodingTransmitIB()
    risk = RiskManager(RiskConfig())
    e = engine(tmp_path, ib, enabled=True, risk_manager=risk, max_entries_per_day=1)
    result = run(e.submit(stock(), request()))
    assert result.reason.startswith("transmission_error_manual_check")
    assert risk.trading_locked is True
    from datetime import datetime, timezone
    assert e.journal.submitted_count_on(datetime.now(timezone.utc).date().isoformat()) == 1
