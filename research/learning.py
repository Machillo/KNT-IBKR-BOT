from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
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
    freshness_factor: float = 1.0


class LearningEngine:
    """Turns accumulated research evidence into bounded strategy confidence.

    The engine may change strategy confidence, but it has no authority over broker
    permissions, kill switches, daily-loss limits, or absolute portfolio-risk limits.
    Research evidence also loses influence as it ages so stale backtests cannot keep
    a strategy permanently trusted or avoided without fresh validation.
    """

    def __init__(self, store: StrategyPerformanceStore) -> None:
        self.store = store

    def _freshness_factor(self, *, symbol: str, asset_class: str, timeframe: str) -> float:
        status_reader = getattr(self.store, "research_status", None)
        if status_reader is None:
            return 1.0
        row = status_reader(symbol=symbol, asset_class=asset_class, timeframe=timeframe)
        if row is None:
            return 1.0
        try:
            researched_at = datetime.fromisoformat(str(row["researched_at"]))
        except (KeyError, TypeError, ValueError):
            return 0.5
        if researched_at.tzinfo is None:
            researched_at = researched_at.replace(tzinfo=timezone.utc)
        age_hours = max(0.0, (datetime.now(timezone.utc) - researched_at).total_seconds() / 3600.0)
        if age_hours <= 24.0:
            return 1.0
        if age_hours <= 72.0:
            return 0.85
        if age_hours <= 168.0:
            return 0.65
        if age_hours <= 720.0:
            return 0.40
        return 0.20

    def _record(
        self,
        *,
        symbol: str,
        asset_class: str,
        timeframe: str,
        regime: str,
        strategy: str,
        assessment: LearningAssessment,
    ) -> None:
        recorder = getattr(self.store, "record_learning_assessment", None)
        if recorder is None:
            return
        recorder(
            symbol=symbol,
            asset_class=asset_class,
            timeframe=timeframe,
            regime=regime,
            strategy=strategy,
            status=assessment.status.value,
            confidence=assessment.confidence,
            selector_bonus=assessment.selector_bonus,
            freshness_factor=assessment.freshness_factor,
            reason=assessment.reason,
            evidence=assessment.evidence,
        )

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
            assessment = LearningAssessment(
                LearningStatus.UNKNOWN, 0.0, 0.0, None, "no_context_evidence", 1.0
            )
            self._record(
                symbol=symbol, asset_class=asset_class, timeframe=timeframe,
                regime=regime, strategy=strategy, assessment=assessment,
            )
            return assessment

        sample_quality = min(1.0, evidence.samples / 6.0)
        trade_quality = min(1.0, evidence.trades / 120.0)
        oos_quality = min(1.0, evidence.oos_samples / 3.0)
        quality = 0.25 * sample_quality + 0.35 * trade_quality + 0.40 * oos_quality

        freshness = self._freshness_factor(
            symbol=symbol, asset_class=asset_class, timeframe=timeframe
        )
        effective_quality = quality * freshness

        directional = max(-1.0, min(1.0, evidence.evidence_score / 20.0))
        confidence = max(0.0, min(100.0, 50.0 + directional * 40.0 * effective_quality))
        selector_bonus = max(-20.0, min(20.0, evidence.evidence_score * effective_quality))

        if evidence.oos_samples == 0 or evidence.trades < 20:
            status = LearningStatus.DEVELOPING
            selector_bonus *= 0.5
            reason = "insufficient_oos_or_trades"
        elif freshness < 0.65:
            status = LearningStatus.DEVELOPING
            reason = "stale_evidence_requires_refresh"
        elif selector_bonus <= -6.0:
            status = LearningStatus.AVOID
            reason = "negative_validated_evidence"
        elif selector_bonus >= 6.0 and evidence.oos_samples >= 2 and evidence.trades >= 60:
            status = LearningStatus.TRUSTED
            reason = "positive_validated_evidence"
        else:
            status = LearningStatus.DEVELOPING
            reason = "evidence_still_developing"

        assessment = LearningAssessment(
            status=status,
            confidence=round(confidence, 2),
            selector_bonus=round(selector_bonus, 2),
            evidence=evidence,
            reason=reason,
            freshness_factor=round(freshness, 2),
        )
        self._record(
            symbol=symbol, asset_class=asset_class, timeframe=timeframe,
            regime=regime, strategy=strategy, assessment=assessment,
        )
        return assessment
