import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from research.coordinator import ResearchCoordinatorResult
from research.performance import StrategyPerformanceStore
from research.scheduler import ContinuousResearchScheduler


class FakeCoordinator:
    def __init__(self, store, freshness_hours=24):
        self.store = store
        self.timeframe = "1 hour"
        self.freshness = timedelta(hours=freshness_hours)
        self.calls = []

    async def research_contract(self, contract):
        self.calls.append(contract.symbol)
        return ResearchCoordinatorResult(contract.symbol, "REFRESHED", 500, "research_completed")


def candidate(symbol: str, score: float):
    contract = SimpleNamespace(symbol=symbol, secType="STK")
    return SimpleNamespace(symbol=symbol, score=score, contract=contract)


def set_research_time(store: StrategyPerformanceStore, symbol: str, when: datetime):
    with store._connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO research_context(symbol, asset_class, timeframe, researched_at, bars) VALUES (?, ?, ?, ?, ?)",
            (symbol, "STK", "1 hour", when.isoformat(), 500),
        )


def test_scheduler_prioritizes_never_researched_before_fresh(tmp_path: Path):
    store = StrategyPerformanceStore(tmp_path / "perf.db")
    coordinator = FakeCoordinator(store)
    now = datetime.now(timezone.utc)
    set_research_time(store, "FRESH", now)

    scheduler = ContinuousResearchScheduler(coordinator, store, max_attempts_per_cycle=2)
    ranked = scheduler.rank([candidate("FRESH", 99), candidate("NEW", 60)])

    assert ranked[0].symbol == "NEW"
    assert ranked[0].reason == "never_researched"
    assert ranked[-1].symbol == "FRESH"
    assert ranked[-1].reason == "research_fresh"


def test_scheduler_prioritizes_stale_research_and_respects_budget(tmp_path: Path):
    store = StrategyPerformanceStore(tmp_path / "perf.db")
    coordinator = FakeCoordinator(store)
    now = datetime.now(timezone.utc)
    set_research_time(store, "STALE", now - timedelta(days=3))
    set_research_time(store, "FRESH", now)

    scheduler = ContinuousResearchScheduler(coordinator, store, max_attempts_per_cycle=1)
    result = asyncio.run(scheduler.run_cycle([
        candidate("FRESH", 100), candidate("STALE", 70), candidate("NEW", 50)
    ]))

    assert result.attempted == 1
    assert result.refreshed == 1
    assert coordinator.calls == ["NEW"]
