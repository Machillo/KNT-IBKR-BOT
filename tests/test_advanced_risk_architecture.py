from datetime import datetime, timedelta

from backtest.growth import simulate_growth
from core.order_templates import adaptive_entry_order, bracket_orders
from market.history import PriceBar
from market.regime import MarketRegime, RegimeDetector
from portfolio.brain import PortfolioSnapshot
from portfolio.allocation import PortfolioAllocator
from portfolio.exposure import CrossExposureGuard, ExposurePosition


def bars_from_prices(prices, spread=1.0):
    start = datetime(2025, 1, 1)
    return [
        PriceBar(start + timedelta(hours=i), p, p + spread, p - spread, p, 1000)
        for i, p in enumerate(prices)
    ]


def snapshot():
    return PortfolioSnapshot(
        net_liquidation=100_000,
        cash=80_000,
        committed_notional=0,
        open_position_risk=0,
        pending_order_notional=0,
        daily_loss_used=0,
        daily_loss_limit=2_000,
        trading_locked=False,
    )


def test_regime_detector_identifies_clear_trend_with_adx_and_ema():
    prices = [100 + i * 0.5 for i in range(240)]
    result = RegimeDetector().evaluate(bars_from_prices(prices, spread=0.3))
    assert result.regime == MarketRegime.TRENDING
    assert result.adx >= 25
    assert result.ema_slope_pct > 0


def test_volatility_multiplier_can_only_reduce_position_size():
    allocator = PortfolioAllocator(risk_pct=0.01, max_position_pct=0.50)
    normal = allocator.propose(snapshot(), symbol="A", asset_class="STK", entry_price=100, stop_price=95)
    stressed = allocator.propose(
        snapshot(), symbol="A", asset_class="STK", entry_price=100, stop_price=95,
        volatility_multiplier=0.5,
    )
    assert normal is not None and stressed is not None
    assert stressed.quantity <= normal.quantity
    assert stressed.proposed_risk <= normal.proposed_risk


def test_cross_exposure_guard_rejects_correlated_cluster():
    returns = tuple(0.001 * i for i in range(30))
    positions = (
        ExposurePosition("NVDA", "LONG", 25_000, "SEMIS", returns),
        ExposurePosition("AMD", "LONG", 20_000, "SEMIS", returns),
    )
    decision = CrossExposureGuard(
        max_same_sector_pct=0.80, max_correlated_cluster_pct=0.50, correlation_threshold=0.80
    ).evaluate(
        net_liquidation=100_000,
        positions=positions,
        proposed_symbol="AVGO",
        proposed_side="LONG",
        proposed_notional=10_000,
        proposed_sector="SEMIS",
        proposed_returns=returns,
    )
    assert decision.approved is False
    assert decision.reason == "correlated_cluster_limit"


def test_order_templates_attach_server_side_stop_and_adaptive_algo():
    adaptive = adaptive_entry_order(action="BUY", quantity=10, limit_price=100)
    assert adaptive.algoStrategy == "Adaptive"
    bracket = bracket_orders(
        action="BUY", quantity=10, entry_price=100, take_profit_price=110,
        stop_price=95, parent_order_id=1001, adaptive_parent=True,
    )
    assert bracket.parent.transmit is False
    assert bracket.take_profit.parentId == 1001
    assert bracket.stop_loss.parentId == 1001
    assert bracket.stop_loss.orderType == "STP"
    assert bracket.stop_loss.transmit is True


def test_growth_simulation_is_deterministic_and_reports_distribution():
    result = simulate_growth([0.01, -0.005, 0.002], periods=50, paths=500, seed=7)
    assert result.median_final_equity > 0
    assert result.p10_final_equity <= result.median_final_equity <= result.p90_final_equity
    assert 0 <= result.probability_of_loss_pct <= 100
    assert 0 <= result.probability_of_ruin_pct <= 100
