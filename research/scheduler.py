from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from research.coordinator import ContinuousResearchCoordinator, ResearchCoordinatorResult
from research.performance import StrategyPerformanceStore
from utils.logger import logger


@dataclass(frozen=True)
class ResearchCandidate:
    contract: object
    symbol: str
    asset_class: str
    opportunity_score: float
    priority: float
    reason: str


@dataclass(frozen=True)
class ResearchCycleResult:
    considered: int
    attempted: int
    refreshed: int
    skipped: int
    results: tuple[ResearchCoordinatorResult, ...]


class ContinuousResearchScheduler:
    """Prioritize and budget recurring research without placing orders.

    The scheduler owns research cadence/priority only. It cannot modify trading
    permissions, kill switches, daily-loss limits, or absolute risk limits.
    """

    def __init__(
        self,
        coordinator: ContinuousResearchCoordinator,
        store: StrategyPerformanceStore,
        *,
        max_attempts_per_cycle: int = 2,
    ) -> None:
        self.coordinator = coordinator
        self.store = store
        self.max_attempts_per_cycle = max(0, int(max_attempts_per_cycle))

    def _priority(self, candidate) -> ResearchCandidate:
        symbol = str(getattr(candidate, "symbol", "") or getattr(candidate.contract, "symbol", "") or "").upper()
        asset_class = str(getattr(candidate.contract, "secType", "") or "STK").upper()
        opportunity_score = float(getattr(candidate, "score", 0.0) or 0.0)
        status = self.store.research_status(
            symbol=symbol,
            asset_class=asset_class,
            timeframe=self.coordinator.timeframe,
        )
        if status is None:
            return ResearchCandidate(candidate.contract, symbol, asset_class, opportunity_score,
                                     1000.0 + opportunity_score, "never_researched")

        try:
            researched_at = datetime.fromisoformat(str(status["researched_at"]))
            if researched_at.tzinfo is None:
                researched_at = researched_at.replace(tzinfo=timezone.utc)
            age_hours = max(0.0, (datetime.now(timezone.utc) - researched_at).total_seconds() / 3600.0)
        except (KeyError, TypeError, ValueError):
            age_hours = 10_000.0

        freshness_hours = max(1.0, self.coordinator.freshness.total_seconds() / 3600.0)
        stale_ratio = age_hours / freshness_hours
        if stale_ratio >= 1.0:
            reason = "stale_research"
            priority = 500.0 + min(500.0, stale_ratio * 50.0) + opportunity_score
        else:
            reason = "research_fresh"
            priority = opportunity_score - 1000.0
        return ResearchCandidate(candidate.contract, symbol, asset_class, opportunity_score, priority, reason)

    def rank(self, candidates: list[object]) -> list[ResearchCandidate]:
        ranked = [self._priority(candidate) for candidate in candidates]
        ranked.sort(key=lambda item: item.priority, reverse=True)
        return ranked

    async def run_cycle(self, candidates: list[object]) -> ResearchCycleResult:
        ranked = self.rank(candidates)
        attempted = refreshed = skipped = 0
        results: list[ResearchCoordinatorResult] = []

        for item in ranked:
            if attempted >= self.max_attempts_per_cycle:
                break
            if item.reason == "research_fresh":
                continue
            attempted += 1
            result = await self.coordinator.research_contract(item.contract)
            results.append(result)
            if result.status == "REFRESHED":
                refreshed += 1
            else:
                skipped += 1
            logger.info(
                "RESEARCH SCHEDULER | symbol=%s priority=%.2f reason=%s status=%s bars=%s cycle=%s/%s",
                item.symbol, item.priority, item.reason, result.status, result.bars,
                attempted, self.max_attempts_per_cycle,
            )

        logger.info(
            "RESEARCH SCHEDULER CYCLE | considered=%s attempted=%s refreshed=%s skipped=%s budget=%s",
            len(ranked), attempted, refreshed, skipped, self.max_attempts_per_cycle,
        )
        return ResearchCycleResult(
            considered=len(ranked), attempted=attempted, refreshed=refreshed,
            skipped=skipped, results=tuple(results),
        )
