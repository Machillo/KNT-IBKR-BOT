from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from research.performance import PerformanceEvidence, StrategyPerformanceStore


class LearningStatus(str, Enum):
    UNKNOWN = "UNKNOWN"
    DEVELOPING = "DEVELOPING"
    TRUSTED = "TRUSTED"
    AVOID = "AVOID"


@dataclass(frozen=True)
class LearningAssessment:
    status: LearningStatus
    confidence: float
    selector_bonus: float
    evidence: PerformanceEvidence | None
    reason: str


class LearningEngine:
    """Turns accumulated research evidence into bounded strategy confidence.

    The engine may change strategy confidence, but it has no authority over broker
    permissions, kill switches, daily-loss limits, or absolute portfolio-risk limits.
    """

    def __init__(self, store: StrategyPerformanceStore) -> None:
        self.store = store

    def assess(
        self,
        *,
        symbol: str,
        asset_class: str,
        timeframe: str,
        regime: str,
        strategy: str,
    ) -> LearningAssessment:
        evidence = self.store.evidence(
            symbol=symbol,
            asset_class=asset_class,
            timeframe=timeframe,
            regime=regime,
            strategy=strategy,
        )
        if evidence is None:
            return LearningAssessment(
                LearningStatus.UNKNOWN, 0.0, 0.0, None, "no_context_evidence"
            )

        # Evidence quality grows with independent samples, trades and OOS coverage.
        sample_quality = min(1.0, evidence.samples / 6.0)
        trade_quality = min(1.0, evidence.trades / 120.0)
        oos_quality = min(1.0, evidence.oos_samples / 3.0)
        quality = 0.25 * sample_quality + 0.35 * trade_quality + 0.40 * oos_quality

        # Convert the store's deliberately bounded [-20,+20] evidence score into
        # directional conviction. Poor evidence is allowed to veto confidence.
        directional = max(-1.0, min(1.0, evidence.evidence_score / 20.0))
        confidence = max(0.0, min(100.0, 50.0 + directional * 40.0 * quality))
        selector_bonus = max(-20.0, min(20.0, evidence.evidence_score * quality))

        if evidence.oos_samples == 0 or evidence.trades < 20:
            status = LearningStatus.DEVELOPING
            selector_bonus *= 0.5
            reason = "insufficient_oos_or_trades"
        elif selector_bonus <= -6.0:
            status = LearningStatus.AVOID
            reason = "negative_validated_evidence"
        elif selector_bonus >= 6.0 and evidence.oos_samples >= 2 and evidence.trades >= 60:
            status = LearningStatus.TRUSTED
            reason = "positive_validated_evidence"
        else:
            status = LearningStatus.DEVELOPING
            reason = "evidence_still_developing"

        return LearningAssessment(
            status=status,
            confidence=round(confidence, 2),
            selector_bonus=round(selector_bonus, 2),
            evidence=evidence,
            reason=reason,
        )
