from research.context_promotions import ContextPromotion, ContextPromotionStore
from research.learning import LearningEngine, LearningStatus
from research.performance import StrategyPerformanceStore


def test_symbol_promotion_can_only_develop_without_exact_research(tmp_path):
    db = tmp_path / "learning.db"
    store = StrategyPerformanceStore(db)
    promotions = ContextPromotionStore(db)
    promotions.record(ContextPromotion(
        scope="symbol", context="COIN", strategy="momentum_gap_v1",
        status="PROMOTE", score=81.3, datasets=3, profitable_pct=100.0,
        stress_survival_pct=83.3, avg_return_pct=13.55,
        avg_drawdown_pct=14.19, avg_profit_factor=1.12,
    ))

    assessment = LearningEngine(store).assess(
        symbol="COIN", asset_class="STK", timeframe="1 hour",
        regime="TRENDING", strategy="momentum_gap_v1",
    )

    assert assessment.status == LearningStatus.DEVELOPING
    assert 0 < assessment.selector_bonus <= 6
    assert assessment.contextual_scope == "symbol"
    assert assessment.contextual_context == "COIN"
    assert assessment.reason == "contextual_backtest_only"


def test_symbol_avoid_context_is_bounded_without_exact_research(tmp_path):
    db = tmp_path / "learning.db"
    store = StrategyPerformanceStore(db)
    promotions = ContextPromotionStore(db)
    promotions.record(ContextPromotion(
        scope="symbol", context="XYZ", strategy="range_v1",
        status="AVOID", score=20.0, datasets=3, profitable_pct=10.0,
        stress_survival_pct=0.0, avg_return_pct=-10.0,
        avg_drawdown_pct=18.0, avg_profit_factor=0.70,
    ))

    assessment = LearningEngine(store).assess(
        symbol="XYZ", asset_class="STK", timeframe="1 hour",
        regime="RANGE", strategy="range_v1",
    )

    assert assessment.status == LearningStatus.DEVELOPING
    assert -6 <= assessment.selector_bonus < 0
    assert assessment.contextual_status == "AVOID"


def test_unknown_context_remains_unknown(tmp_path):
    store = StrategyPerformanceStore(tmp_path / "learning.db")
    assessment = LearningEngine(store).assess(
        symbol="UNSEEN", asset_class="STK", timeframe="1 hour",
        regime="MIXED", strategy="breakout_v1",
    )
    assert assessment.status == LearningStatus.UNKNOWN
    assert assessment.selector_bonus == 0.0
