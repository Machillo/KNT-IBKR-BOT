from portfolio.brain import PortfolioBrain, PortfolioOpportunity, PortfolioSnapshot


def snapshot(**overrides):
    values = dict(
        net_liquidation=100_000.0,
        cash=80_000.0,
        committed_notional=20_000.0,
        open_position_risk=1_000.0,
        pending_order_notional=0.0,
        daily_loss_used=1_000.0,
        daily_loss_limit=10_000.0,
        trading_locked=False,
    )
    values.update(overrides)
    return PortfolioSnapshot(**values)


def opportunity(**overrides):
    values = dict(
        symbol="NVDA",
        asset_class="STK",
        proposed_notional=10_000.0,
        proposed_risk=1_000.0,
        correlation_to_portfolio=0.25,
    )
    values.update(overrides)
    return PortfolioOpportunity(**values)


def test_portfolio_brain_approves_candidate_with_capacity():
    decision = PortfolioBrain().evaluate(snapshot(), opportunity())
    assert decision.approved is True
    assert decision.reason == "portfolio_capacity_available"
    assert decision.projected_exposure_pct == 0.30


def test_portfolio_brain_rejects_when_trading_locked():
    decision = PortfolioBrain().evaluate(snapshot(trading_locked=True), opportunity())
    assert decision.approved is False
    assert decision.reason == "trading_locked"


def test_portfolio_brain_rejects_gross_exposure_limit():
    brain = PortfolioBrain(max_gross_exposure_pct=0.50)
    decision = brain.evaluate(snapshot(committed_notional=45_000.0), opportunity())
    assert decision.approved is False
    assert decision.reason == "gross_exposure_limit"


def test_portfolio_brain_rejects_daily_loss_budget_exhaustion():
    decision = PortfolioBrain().evaluate(
        snapshot(daily_loss_used=9_500.0, daily_loss_limit=10_000.0),
        opportunity(proposed_risk=1_000.0),
    )
    assert decision.approved is False
    assert decision.reason == "daily_loss_budget_exhausted"


def test_portfolio_brain_rejects_high_correlation():
    decision = PortfolioBrain(max_correlation=0.80).evaluate(
        snapshot(), opportunity(correlation_to_portfolio=0.91)
    )
    assert decision.approved is False
    assert decision.reason == "correlation_limit"


def test_portfolio_brain_rejects_cash_reserve_breach():
    decision = PortfolioBrain(cash_reserve_pct=0.10).evaluate(
        snapshot(cash=15_000.0), opportunity(proposed_notional=10_000.0)
    )
    assert decision.approved is False
    assert decision.reason == "cash_reserve_limit"
