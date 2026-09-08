from dataclasses import replace

from research.learning import LearningEngine, LearningStatus
from research.performance import PerformanceEvidence


class FakeStore:
    def __init__(self, evidence):
        self.value = evidence

    def evidence(self, **kwargs):
        return self.value


def evidence(**changes):
    base = PerformanceEvidence(
        samples=8,
        trades=160,
        mean_return_pct=8.0,
        mean_drawdown_pct=5.0,
        mean_win_rate_pct=55.0,
        mean_profit_factor=1.4,
        mean_sharpe=1.2,
        oos_samples=4,
        evidence_score=12.0,
    )
    return replace(base, **changes)


def assess(value):
    return LearningEngine(FakeStore(value)).assess(
        symbol="NVDA", asset_class="STK", timeframe="1 hour",
        regime="TRENDING", strategy="momentum_gap_v1",
    )


def test_unknown_context_has_no_bonus():
    result = assess(None)
    assert result.status == LearningStatus.UNKNOWN
    assert result.confidence == 0.0
    assert result.selector_bonus == 0.0


def test_positive_validated_evidence_can_be_trusted():
    result = assess(evidence())
    assert result.status == LearningStatus.TRUSTED
    assert 50.0 < result.confidence <= 100.0
    assert 0.0 < result.selector_bonus <= 20.0


def test_negative_validated_evidence_can_be_avoided():
    result = assess(evidence(evidence_score=-15.0, mean_return_pct=-10.0))
    assert result.status == LearningStatus.AVOID
    assert result.selector_bonus <= -6.0


def test_no_oos_stays_developing_and_is_discounted():
    result = assess(evidence(oos_samples=0, trades=100, evidence_score=12.0))
    assert result.status == LearningStatus.DEVELOPING
    assert 0.0 < result.selector_bonus < 12.0


def test_learning_bonus_is_hard_bounded():
    result = assess(evidence(evidence_score=999.0))
    assert result.selector_bonus <= 20.0
    assert result.confidence <= 100.0
