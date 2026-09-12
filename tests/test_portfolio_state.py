from types import SimpleNamespace

from core.account import AccountSnapshot
from portfolio.state import PortfolioStateService


class FakeTrade:
    def __init__(self, symbol: str, qty: float, price: float, *, filled: float = 0.0, account: str = "DU1"):
        self.contract = SimpleNamespace(symbol=symbol, localSymbol=symbol, secType="STK")
        self.order = SimpleNamespace(account=account, totalQuantity=qty, lmtPrice=price, auxPrice=0.0)
        self.orderStatus = SimpleNamespace(filled=filled)

    def isDone(self):
        return False


class FakeIB:
    def __init__(self):
        self._portfolio = [
            SimpleNamespace(
                contract=SimpleNamespace(symbol="AAPL", localSymbol="AAPL", secType="STK"),
                position=10,
                marketPrice=200.0,
                marketValue=2000.0,
            ),
            SimpleNamespace(
                contract=SimpleNamespace(symbol="TSLA", localSymbol="TSLA", secType="STK"),
                position=-5,
                marketPrice=250.0,
                marketValue=-1250.0,
            ),
        ]
        self._trades = [
            FakeTrade("NVDA", 8, 100.0, filled=3.0),
            FakeTrade("MSFT", 2, 300.0, account="OTHER"),
        ]

    def portfolio(self, account):
        assert account == "DU1"
        return self._portfolio

    def openTrades(self):
        return self._trades

    def ticker(self, contract):
        return SimpleNamespace(marketPrice=lambda: 0.0)


def test_portfolio_state_builds_real_exposure_from_broker_objects():
    account = AccountSnapshot(
        account="DU1",
        currency="USD",
        net_liquidation=10000.0,
        total_cash_value=7000.0,
        available_funds=9000.0,
        buying_power=36000.0,
        positions=2,
        open_orders=1,
    )
    state = PortfolioStateService(FakeIB()).build(
        account,
        starting_equity=10500.0,
        daily_loss_limit_pct=0.10,
        trading_locked=False,
    )

    assert len(state.positions) == 2
    assert state.snapshot.committed_notional == 3250.0
    assert state.snapshot.pending_order_notional == 500.0
    assert state.snapshot.daily_loss_used == 500.0
    assert state.snapshot.daily_loss_limit == 1050.0
    assert round(state.snapshot.gross_exposure_pct, 4) == 0.325


def test_portfolio_state_rejects_missing_net_liquidation():
    account = AccountSnapshot(
        account="DU1", currency="USD", net_liquidation=None,
        total_cash_value=0.0, available_funds=0.0, buying_power=0.0,
        positions=0, open_orders=0,
    )
    service = PortfolioStateService(FakeIB())
    try:
        service.build(account, starting_equity=10000.0, daily_loss_limit_pct=0.10, trading_locked=False)
    except RuntimeError as exc:
        assert "NetLiquidation" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")
