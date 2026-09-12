from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from backtest.engine import BacktestEngine
from research.performance import StrategyPerformanceStore
from research.walkforward import WalkForwardResearch
from strategies.library import SINGLE_ASSET_STRATEGIES
from strategies.momentum import MomentumStrategy
from utils.logger import logger


@dataclass(frozen=True)
class ResearchCoordinatorResult:
    symbol: str
    status: str
    bars: int
    reason: str
    run_id: int | None = None


class ContinuousResearchCoordinator:
    """Bounded append-only research worker for market-funnel candidates."""

    def __init__(
        self,
        history_service,
        store: StrategyPerformanceStore,
        *,
        freshness_hours: int = 24,
        duration: str = "365 D",
        timeframe: str = "1 hour",
        min_bars: int = 320,
    ) -> None:
        self.history = history_service
        self.store = store
        self.freshness = timedelta(hours=max(1, freshness_hours))
        self.duration = duration
        self.timeframe = timeframe
        self.min_bars = max(320, min_bars)

    def _is_fresh(self, *, symbol: str, asset_class: str) -> bool:
        row = self.store.research_status(
            symbol=symbol, asset_class=asset_class, timeframe=self.timeframe
        )
        if row is None:
            return False
        try:
            researched_at = datetime.fromisoformat(str(row["researched_at"]))
        except (TypeError, ValueError):
            return False
        if researched_at.tzinfo is None:
            researched_at = researched_at.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - researched_at <= self.freshness

    @staticmethod
    def _bar_time(value) -> str | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.isoformat()
        return str(value)

    async def research_contract(self, contract) -> ResearchCoordinatorResult:
        symbol = str(getattr(contract, "symbol", "") or "").upper()
        asset_class = str(getattr(contract, "secType", "") or "STK").upper()
        if not symbol:
            return ResearchCoordinatorResult("", "SKIPPED", 0, "missing_symbol")
        if self._is_fresh(symbol=symbol, asset_class=asset_class):
            return ResearchCoordinatorResult(symbol, "SKIPPED", 0, "research_fresh")

        bars = await self.history.bars(contract, duration=self.duration, bar_size=self.timeframe)
        if len(bars) < self.min_bars:
            return ResearchCoordinatorResult(symbol, "SKIPPED", len(bars), "insufficient_history")

        run_id = self.store.begin_research_run(
            symbol=symbol,
            asset_class=asset_class,
            timeframe=self.timeframe,
            bars=len(bars),
            dataset_start=self._bar_time(bars[0].time) if bars else None,
            dataset_end=self._bar_time(bars[-1].time) if bars else None,
            source="RESEARCH",
            strategy_version="v1",
        )
        try:
            research = WalkForwardResearch(self.store, BacktestEngine())
            strategies = [MomentumStrategy(), *[factory() for factory in SINGLE_ASSET_STRATEGIES]]
            summaries = research.evaluate(
                symbol=symbol,
                asset_class=asset_class,
                timeframe=self.timeframe,
                bars=bars,
                strategies=strategies,
                run_id=run_id,
                source="RESEARCH",
                strategy_version="v1",
            )
            if not any(item.oos_windows > 0 for item in summaries):
                self.store.finish_research_run(run_id, status="FAILED", notes="no_oos_windows")
                return ResearchCoordinatorResult(symbol, "SKIPPED", len(bars), "no_oos_windows", run_id)

            self.store.finish_research_run(run_id, status="COMPLETED")
            self.store.mark_researched(
                symbol=symbol, asset_class=asset_class, timeframe=self.timeframe,
                bars=len(bars), run_id=run_id,
            )
            logger.info(
                "LEARNING RESEARCH | symbol=%s bars=%s strategies=%s run_id=%s status=REFRESHED",
                symbol, len(bars), len(strategies), run_id,
            )
            return ResearchCoordinatorResult(symbol, "REFRESHED", len(bars), "research_completed", run_id)
        except Exception as exc:
            self.store.finish_research_run(run_id, status="FAILED", notes=str(exc)[:500])
            raise
