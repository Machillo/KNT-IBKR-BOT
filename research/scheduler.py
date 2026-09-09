from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from research.coordinator import ContinuousResearchCoordinator, ResearchCoordinatorResult
from research.cycle_metrics import ResearchCycleMetricsStore
from research.performance import StrategyPerformanceStore
from research.scheduler_state import ResearchSchedulerStateStore
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
    mode: str = "OPEN"


class ContinuousResearchScheduler:
    """Prioritize and budget recurring research without placing orders.

    The scheduler owns research cadence/priority only. It cannot modify trading
    permissions, kill switches, daily-loss limits, or absolute risk limits.
    Repeated research failures are persisted and exponentially backed off so KNT
    does not hammer IBKR or waste cycle budget on the same bad candidate.
    Very stale contexts receive an aging boost so repeated high-scoring new names
    cannot starve old evidence forever.
    """

    def __init__(
        self,
        coordinator: ContinuousResearchCoordinator,
        store: StrategyPerformanceStore,
        *,
        max_attempts_per_cycle: int = 2,
        closed_market_attempts_per_cycle: int | None = None,
        base_backoff_minutes: int = 30,
        max_backoff_hours: int = 24,
        starvation_after_freshness_windows: float = 4.0,
        starvation_priority_boost: float = 600.0,
    ) -> None:
        self.coordinator = coordinator
        self.store = store
        self.max_attempts_per_cycle = max(0, int(max_attempts_per_cycle))
        default_closed = max(self.max_attempts_per_cycle, 4)
        self.closed_market_attempts_per_cycle = max(
            self.max_attempts_per_cycle,
            int(default_closed if closed_market_attempts_per_cycle is None else closed_market_attempts_per_cycle),
        )
        self.base_backoff_minutes = max(1, int(base_backoff_minutes))
        self.max_backoff_hours = max(1, int(max_backoff_hours))
        self.starvation_after_freshness_windows = max(1.0, float(starvation_after_freshness_windows))
        self.starvation_priority_boost = max(0.0, float(starvation_priority_boost))
        self.state = ResearchSchedulerStateStore(store.path)
        self.metrics = ResearchCycleMetricsStore(store.path)

    def _priority(self, candidate) -> ResearchCandidate:
        symbol = str(getattr(candidate, "symbol", "") or getattr(candidate.contract, "symbol", "") or "").upper()
        asset_class = str(getattr(candidate.contract, "secType", "") or "STK").upper()
        opportunity_score = float(getattr(candidate, "score", 0.0) or 0.0)

        if self.state.in_backoff(symbol=symbol, asset_class=asset_class, timeframe=self.coordinator.timeframe):
            return ResearchCandidate(candidate.contract, symbol, asset_class, opportunity_score,
                                     -2000.0 + opportunity_score, "research_backoff")

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
        if stale_ratio >= self.starvation_after_freshness_windows:
            reason = "starved_stale_research"
            age_component = min(500.0, stale_ratio * 50.0)
            priority = 500.0 + age_component + self.starvation_priority_boost + opportunity_score
        elif stale_ratio >= 1.0:
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

    async def run_cycle(self, candidates: list[object], *, market_open: bool = True) -> ResearchCycleResult:
        mode = "OPEN" if market_open else "CLOSED"
        budget = self.max_attempts_per_cycle if market_open else self.closed_market_attempts_per_cycle
        ranked = self.rank(candidates)
        attempted = refreshed = skipped = 0
        results: list[ResearchCoordinatorResult] = []

        for item in ranked:
            if attempted >= budget:
                break
            if item.reason in {"research_fresh", "research_backoff"}:
                continue

            attempted += 1
            try:
                result = await self.coordinator.research_contract(item.contract)
            except Exception as exc:
                reason = f"exception:{type(exc).__name__}"
                result = ResearchCoordinatorResult(item.symbol, "ERROR", 0, reason)
                logger.warning("RESEARCH SCHEDULER ERROR | symbol=%s error=%s", item.symbol, exc)

            results.append(result)
            if result.status == "REFRESHED":
                refreshed += 1
                self.state.record_success(
                    symbol=item.symbol,
                    asset_class=item.asset_class,
                    timeframe=self.coordinator.timeframe,
                    reason=result.reason,
                )
            else:
                skipped += 1
                if result.reason not in {"research_fresh", "missing_symbol"}:
                    self.state.record_failure(
                        symbol=item.symbol,
                        asset_class=item.asset_class,
                        timeframe=self.coordinator.timeframe,
                        status=result.status,
                        reason=result.reason,
                        base_backoff_minutes=self.base_backoff_minutes,
                        max_backoff_hours=self.max_backoff_hours,
                    )

            logger.info(
                "RESEARCH SCHEDULER | mode=%s symbol=%s priority=%.2f reason=%s status=%s bars=%s cycle=%s/%s",
                mode, item.symbol, item.priority, item.reason, result.status, result.bars,
                attempted, budget,
            )

        self.metrics.record(
            mode=mode,
            considered=len(ranked),
            attempted=attempted,
            refreshed=refreshed,
            skipped=skipped,
            budget=budget,
        )
        logger.info(
            "RESEARCH SCHEDULER CYCLE | mode=%s considered=%s attempted=%s refreshed=%s skipped=%s budget=%s",
            mode, len(ranked), attempted, refreshed, skipped, budget,
        )
        return ResearchCycleResult(
            considered=len(ranked), attempted=attempted, refreshed=refreshed,
            skipped=skipped, results=tuple(results), mode=mode,
        )
