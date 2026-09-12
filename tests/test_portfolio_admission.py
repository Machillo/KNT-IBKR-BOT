from config.config import RiskConfig
from portfolio.admission import PortfolioAdmissionCoordinator, RiskDecisionStore
from portfolio.brain import PortfolioSnapshot
from portfolio.state import PortfolioState, PositionExposure
from risk.risk_manager import RiskManager


def state(*, positions=()):
    return PortfolioState(
        snapshot=PortfolioSnapshot(
            net_liquidation=100_000,
            cash=80_000,
            committed_notional=sum(p.notional for p in positions),
            open_position_risk=0,
            pending_order_notional=0,
            daily_loss_used=0,
            daily_loss_limit=2_000,
            trading_locked=False,
        ),
        positions=tuple(positions),
        pending_orders=(),
    )


def coordinator():
    risk = RiskManager(RiskConfig(max_trade_risk_pct=0.01, max_daily_loss_pct=0.02, max_position_pct=0.10))
    return PortfolioAdmissionCoordinator(risk)


def test_flat_portfolio_can_pass_all_admission_layers():
    decision = coordinator().evaluate(
        state=state(), symbol="SPY", asset_class="STK", side="LONG", quantity=50,
        entry_price=100, stop_price=98, proposed_notional=5_000, proposed_risk=100,
        candidate_returns=tuple(0.001 * i for i in range(60)), position_returns={},
        correlation_to_portfolio=0.0,
    )
    assert decision.approved is True
    assert decision.reason == "portfolio_risk_approved"


def test_existing_position_without_correlation_fails_closed():
    position = PositionExposure("NVDA", "STK", 100, 100, 10_000, con_id=1)
    decision = coordinator().evaluate(
        state=state(positions=(position,)), symbol="AMD", asset_class="STK", side="LONG", quantity=50,
        entry_price=100, stop_price=98, proposed_notional=5_000, proposed_risk=100,
        candidate_returns=tuple(0.001 * i for i in range(60)), position_returns={},
        correlation_to_portfolio=None,
    )
    assert decision.approved is False
    assert decision.reason == "correlation_unavailable"


def test_hard_risk_limit_cannot_be_overridden_by_portfolio_layer():
    decision = coordinator().evaluate(
        state=state(), symbol="SPY", asset_class="STK", side="LONG", quantity=200,
        entry_price=100, stop_price=90, proposed_notional=20_000, proposed_risk=2_000,
        candidate_returns=(), position_returns={}, correlation_to_portfolio=0.0,
    )
    assert decision.approved is False
    assert decision.reason.startswith("hard_risk:")


def test_risk_decisions_are_append_only(tmp_path):
    store = RiskDecisionStore(tmp_path / "risk.db")
    first = store.record(
        symbol="SPY", side="LONG", strategy="breakout_v1", approved=False,
        reason="correlation_unavailable", quantity=10, entry_price=100, stop_price=95,
        proposed_notional=1000, proposed_risk=50, correlation=None, regime="TRENDING",
    )
    second = store.record(
        symbol="SPY", side="LONG", strategy="breakout_v1", approved=True,
        reason="portfolio_risk_approved", quantity=10, entry_price=100, stop_price=95,
        proposed_notional=1000, proposed_risk=50, correlation=0.2, regime="TRENDING",
    )
    assert second > first
