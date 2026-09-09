from types import SimpleNamespace

from execution.paper import PaperExecutionEngine, PaperExecutionRequest, TradeJournalStore


class FakeTrade:
    def __init__(self, symbol="AAPL", account="DUP"):
        self.contract = SimpleNamespace(localSymbol=symbol, symbol=symbol)
        self.order = SimpleNamespace(account=account, orderId=1)

    def isDone(self):
        return False


class FakeIB:
    def __init__(self, *, positions=(), trades=()):
        self._positions = positions
        self._trades = trades
        self.submitted = []

    def portfolio(self, account):
        return list(self._positions)

    def openTrades(self):
        return list(self._trades)

    def bracketOrder(self, action, quantity, entry, target, stop, **kwargs):
        parent = SimpleNamespace(orderId=101, account="", algoStrategy="", algoParams=[], transmit=False)
        tp = SimpleNamespace(orderId=102, account="", transmit=False)
        sl = SimpleNamespace(orderId=103, account="", transmit=True)
        return [parent, tp, sl]

    def placeOrder(self, contract, order):
        trade = SimpleNamespace(
            contract=contract,
            order=order,
            orderStatus=SimpleNamespace(status="Submitted", filled=0, remaining=1, avgFillPrice=0),
            statusEvent=SimpleNamespace(__iadd__=lambda self, cb: self),
            fillEvent=SimpleNamespace(__iadd__=lambda self, cb: self),
        )
        self.submitted.append((contract, order))
        return trade


def request():
    return PaperExecutionRequest(
        symbol="AAPL", strategy="momentum_v1", side="LONG", quantity=10,
        entry_price=100, stop_price=95, target_price=110, regime="TRENDING",
    )


def test_paper_execution_is_disabled_by_default(tmp_path):
    engine = PaperExecutionEngine(FakeIB(), account="PAPER", enabled=False, journal=TradeJournalStore(tmp_path / "j.db"))
    result = engine.submit(SimpleNamespace(symbol="AAPL"), request())
    assert result.submitted is False
    assert result.reason == "autonomous_paper_disabled"


def test_paper_execution_rejects_duplicate_position(tmp_path):
    position = SimpleNamespace(contract=SimpleNamespace(localSymbol="AAPL", symbol="AAPL"), position=5)
    engine = PaperExecutionEngine(FakeIB(positions=(position,)), account="PAPER", enabled=True, journal=TradeJournalStore(tmp_path / "j.db"))
    result = engine.submit(SimpleNamespace(symbol="AAPL"), request())
    assert result.submitted is False
    assert result.reason == "duplicate_symbol_exposure"


def test_paper_execution_rejects_duplicate_open_order(tmp_path):
    engine = PaperExecutionEngine(FakeIB(trades=(FakeTrade("AAPL", "PAPER"),)), account="PAPER", enabled=True, journal=TradeJournalStore(tmp_path / "j.db"))
    result = engine.submit(SimpleNamespace(symbol="AAPL"), request())
    assert result.submitted is False
    assert result.reason == "duplicate_symbol_exposure"
