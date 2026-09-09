from portfolio.allocation import PortfolioAllocator
from portfolio.brain import PortfolioSnapshot


def snapshot(**overrides):
    values = dict(
        net_liquidation=100_000.0,
        cash=100_000.0,
        committed_notional=0.0,
        open_position_risk=0.0,
        pending_order_notional=0.0,
        daily_loss_used=0.0,
        daily_loss_limit=10_000.0,
        trading_locked=False,
    )
    values.update(overrides)
    return PortfolioSnapshot(**values)


def test_allocator_caps_by_notional():
    proposal = PortfolioAllocator(risk_pct=0.05, max_position_pct=0.10).propose(
        snapshot(), symbol="NVDA", asset_class="STK", entry_price=100.0, stop_price=99.0
    )
    assert proposal is not None
    assert proposal.quantity == 100.0
    assert proposal.proposed_notional == 10_000.0
    assert proposal.proposed_risk == 100.0


def test_allocator_caps_by_risk():
    proposal = PortfolioAllocator(risk_pct=0.01, max_position_pct=0.50).propose(
        snapshot(), symbol="NVDA", asset_class="STK", entry_price=100.0, stop_price=90.0
    )
    assert proposal is not None
    assert proposal.quantity == 100.0
    assert proposal.proposed_risk == 1_000.0


def test_allocator_respects_remaining_daily_loss_budget():
    proposal = PortfolioAllocator(risk_pct=0.05, max_position_pct=0.50).propose(
        snapshot(daily_loss_used=9_500.0, daily_loss_limit=10_000.0),
        symbol="NVDA", asset_class="STK", entry_price=100.0, stop_price=90.0,
    )
    assert proposal is not None
    assert proposal.proposed_risk == 500.0


def test_allocator_fails_closed_when_locked():
    assert PortfolioAllocator().propose(
        snapshot(trading_locked=True), symbol="NVDA", asset_class="STK",
        entry_price=100.0, stop_price=95.0,
    ) is None
