from pathlib import Path

from portfolio.brain import PortfolioSnapshot
from portfolio.history import PortfolioHistoryStore
from portfolio.state import PortfolioState, PositionExposure, PendingOrderExposure


def test_portfolio_history_persists_append_only_snapshots(tmp_path: Path):
    store = PortfolioHistoryStore(tmp_path / "portfolio.db")
    state = PortfolioState(
        snapshot=PortfolioSnapshot(
            net_liquidation=100000.0,
            cash=80000.0,
            committed_notional=15000.0,
            open_position_risk=0.0,
            pending_order_notional=5000.0,
            daily_loss_used=1200.0,
            daily_loss_limit=10000.0,
            trading_locked=False,
        ),
        positions=(PositionExposure("AAPL", "STK", 50.0, 200.0, 10000.0),),
        pending_orders=(PendingOrderExposure("MSFT", "STK", 10.0, 500.0, 5000.0),),
    )

    store.record(account="DU123", state=state)
    store.record(account="DU123", state=state)
    rows = store.recent("DU123", limit=10)

    assert len(rows) == 2
    assert rows[0].account == "DU123"
    assert rows[0].committed_notional == 15000.0
    assert rows[0].pending_order_notional == 5000.0
    assert rows[0].position_count == 1
    assert rows[0].pending_order_count == 1
    assert rows[0].gross_exposure_pct == 0.15
