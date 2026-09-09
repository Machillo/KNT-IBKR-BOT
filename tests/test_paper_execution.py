import asyncio
from types import SimpleNamespace

from execution.paper import PaperExecutionEngine, PaperExecutionRequest, TradeJournalStore


class EventHook:
    def __init__(self):
        self.callbacks = []

    def __iadd__(self, callback):
        self.callbacks.append(callback)
        return self


class FakeTrade:
    def __init__(self, symbol="AAPL", account="DUP", order=None, status="Submitted", log=()):
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
    def __init__(self, *, positions=(), trades=(), reject_index=None):
        self._positions = positions
        self._trades = trades
        self.submitted = []
        self.bracket_args = None
        self.reject_index = reject_index

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


def request(**overrides):
    values = dict(
        symbol="AAPL", strategy="momentum_v1", side="LONG", quantity=10,
        entry_price=100, stop_price=95, target_price=110, regime="TRENDING",
    )
    values.update(overrides)
    return PaperExecutionRequest(**values)


def engine(tmp_path, ib=None, **kwargs):
    return PaperExecutionEngine(
        ib or FakeIB(),
        account="PAPER",
        journal=TradeJournalStore(tmp_path / "j.db"),
        paper_authorized=True,
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


def test_paper_execution_requires_explicit_paper_authorization(tmp_path):
    e = PaperExecutionEngine(
        FakeIB(), account="PAPER", enabled=True, paper_authorized=False,
        acceptance_delay_seconds=0, journal=TradeJournalStore(tmp_path / "j.db"),
    )
    result = run(e.submit(stock(), request()))
    assert result.submitted is False
    assert result.reason == "paper_execution_not_authorized"


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
    result = run(engine(tmp_path, FakeIB(trades=(FakeTrade("AAPL", "PAPER"),)), enabled=True).submit(stock(), request()))
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
        request(symbol="INTC", entry_price=105.98, stop_price=102.8675, target_price=111.16750000000002),
    ))
    assert result.submitted is True
    assert result.reason == "bracket_confirmed"
    assert result.parent_order_id == 101
    assert ib.bracket_args[:5] == ("BUY", 10, 105.98, 111.17, 102.87)
    assert len(ib.submitted) == 3
    parent, tp, sl = [order for _, order, _ in ib.submitted]
    assert parent.account == "PAPER"
    assert tp.account == "PAPER"
    assert sl.account == "PAPER"
    assert parent.algoStrategy == "Adaptive"


def test_paper_execution_short_geometry_and_mapping(tmp_path):
    ib = FakeIB()
    result = run(engine(tmp_path, ib, enabled=True).submit(
        stock("AAPL"), request(side="SHORT", entry_price=100.001, stop_price=105.004, target_price=95.002),
    ))
    assert result.submitted is True
    assert ib.bracket_args[:5] == ("SELL", 10, 100.0, 95.0, 105.0)


def test_paper_execution_fails_when_child_leg_is_rejected(tmp_path):
    ib = FakeIB(reject_index=1)
    result = run(engine(tmp_path, ib, enabled=True).submit(stock(), request()))
    assert result.submitted is False
    assert result.reason in {"order_status_cancelled", "broker_error_110"}
