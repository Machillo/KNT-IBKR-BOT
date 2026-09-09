import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from research.coordinator import ResearchCoordinatorResult
from research.performance import StrategyPerformanceStore
from research.scheduler import ContinuousResearchScheduler


class FakeCoordinator:
    def __init__(self, store, freshness_hours=24, result=None, error=None):
        self.store = store
        self.timeframe = "1 hour"
        self.freshness = timedelta(hours=freshness_hours)
        self.calls = []
        self.result = result
        self.error = error

    async def research_contract(self, contract):
        self.calls.append(contract.symbol)
        if self.error is not None:
            raise self.error
        if self.result is not None:
            return ResearchCoordinatorResult(
                contract.symbol, self.result.status, self.result.bars, self.result.reason
            )
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
    assert result.mode == "OPEN"
    assert coordinator.calls == ["NEW"]


def test_scheduler_prevents_starvation_of_very_stale_research(tmp_path: Path):
    store = StrategyPerformanceStore(tmp_path / "perf.db")
    coordinator = FakeCoordinator(store, freshness_hours=24)
    now = datetime.now(timezone.utc)
    set_research_time(store, "STARVED", now - timedelta(days=10))

    scheduler = ContinuousResearchScheduler(
        coordinator,
        store,
        max_attempts_per_cycle=1,
        starvation_after_freshness_windows=4,
        starvation_priority_boost=600,
    )
    ranked = scheduler.rank([
        candidate("NEW", 100),
        candidate("STARVED", 5),
    ])

    assert ranked[0].symbol == "STARVED"
    assert ranked[0].reason == "starved_stale_research"
    result = asyncio.run(scheduler.run_cycle([
        candidate("NEW", 100),
        candidate("STARVED", 5),
    ]))
    assert result.attempted == 1
    assert coordinator.calls == ["STARVED"]


def test_scheduler_persists_failure_backoff_and_skips_retry(tmp_path: Path):
    store = StrategyPerformanceStore(tmp_path / "perf.db")
    failed = ResearchCoordinatorResult("BAD", "SKIPPED", 12, "insufficient_history")
    coordinator = FakeCoordinator(store, result=failed)
    scheduler = ContinuousResearchScheduler(
        coordinator, store, max_attempts_per_cycle=2, base_backoff_minutes=30
    )

    first = asyncio.run(scheduler.run_cycle([candidate("BAD", 90)]))
    assert first.attempted == 1
    state = scheduler.state.get(symbol="BAD", asset_class="STK", timeframe="1 hour")
    assert state is not None
    assert state.consecutive_failures == 1
    assert state.last_reason == "insufficient_history"
    assert state.next_retry_at is not None

    second = asyncio.run(scheduler.run_cycle([candidate("BAD", 90)]))
    assert second.attempted == 0
    assert scheduler.rank([candidate("BAD", 90)])[0].reason == "research_backoff"
    assert coordinator.calls == ["BAD"]


def test_scheduler_exception_counts_once_and_enters_backoff(tmp_path: Path):
    store = StrategyPerformanceStore(tmp_path / "perf.db")
    coordinator = FakeCoordinator(store, error=TimeoutError("IBKR timeout"))
    scheduler = ContinuousResearchScheduler(coordinator, store, max_attempts_per_cycle=1)

    result = asyncio.run(scheduler.run_cycle([candidate("TIMEOUT", 80)]))
    assert result.attempted == 1
    assert result.skipped == 1
    assert result.results[0].status == "ERROR"

    state = scheduler.state.get(symbol="TIMEOUT", asset_class="STK", timeframe="1 hour")
    assert state is not None
    assert state.consecutive_failures == 1
    assert state.last_reason == "exception:TimeoutError"


def test_scheduler_success_resets_previous_failure_state(tmp_path: Path):
    store = StrategyPerformanceStore(tmp_path / "perf.db")
    coordinator = FakeCoordinator(store)
    scheduler = ContinuousResearchScheduler(coordinator, store, max_attempts_per_cycle=1)
    scheduler.state.record_failure(
        symbol="RECOVER", asset_class="STK", timeframe="1 hour",
        status="ERROR", reason="exception:TimeoutError", base_backoff_minutes=1,
    )
    with scheduler.state._connect() as conn:
        conn.execute(
            "UPDATE research_scheduler_state SET next_retry_at=? WHERE symbol='RECOVER'",
            ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),),
        )

    result = asyncio.run(scheduler.run_cycle([candidate("RECOVER", 75)]))
    assert result.refreshed == 1
    state = scheduler.state.get(symbol="RECOVER", asset_class="STK", timeframe="1 hour")
    assert state is not None
    assert state.consecutive_failures == 0
    assert state.next_retry_at is None
    assert state.last_status == "REFRESHED"


def test_closed_market_cycle_uses_larger_research_budget(tmp_path: Path):
    store = StrategyPerformanceStore(tmp_path / "perf.db")
    coordinator = FakeCoordinator(store)
    scheduler = ContinuousResearchScheduler(
        coordinator, store, max_attempts_per_cycle=1, closed_market_attempts_per_cycle=3
    )

    result = asyncio.run(scheduler.run_cycle(
        [candidate("A", 90), candidate("B", 80), candidate("C", 70)], market_open=False
    ))

    assert result.mode == "CLOSED"
    assert result.attempted == 3
    assert result.refreshed == 3
    assert coordinator.calls == ["A", "B", "C"]


def test_scheduler_persists_cycle_metrics(tmp_path: Path):
    store = StrategyPerformanceStore(tmp_path / "perf.db")
    coordinator = FakeCoordinator(store)
    scheduler = ContinuousResearchScheduler(coordinator, store, max_attempts_per_cycle=2)

    asyncio.run(scheduler.run_cycle([candidate("A", 90), candidate("B", 80)]))
    metrics = scheduler.metrics.recent(1)

    assert len(metrics) == 1
    assert metrics[0].mode == "OPEN"
    assert metrics[0].considered == 2
    assert metrics[0].attempted == 2
    assert metrics[0].refreshed == 2
    assert metrics[0].budget == 2
