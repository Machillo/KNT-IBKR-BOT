from dataclasses import replace
from datetime import datetime, timedelta, timezone

from research.learning import LearningEngine, LearningStatus
from research.performance import PerformanceEvidence


class FakeStore:
    def __init__(self, evidence, researched_at=None):
        self.value = evidence
        self.researched_at = researched_at

    def evidence(self, **kwargs):
        return self.value

    def research_status(self, **kwargs):
        if self.researched_at is None:
            return None
        return {"researched_at": self.researched_at.isoformat()}


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


def assess(value, researched_at=None):
    return LearningEngine(FakeStore(value, researched_at)).assess(
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


def test_fresh_research_keeps_full_learning_weight():
    result = assess(evidence(), datetime.now(timezone.utc) - timedelta(hours=12))
    assert result.freshness_factor == 1.0
    assert result.status == LearningStatus.TRUSTED


def test_stale_research_loses_authority_until_refreshed():
    result = assess(evidence(), datetime.now(timezone.utc) - timedelta(days=10))
    assert result.freshness_factor == 0.4
    assert result.status == LearningStatus.DEVELOPING
    assert result.reason == "stale_evidence_requires_refresh"
    assert 0.0 < result.selector_bonus < 6.0
