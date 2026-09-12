from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from market.history import PriceBar
from research.coordinator import ContinuousResearchCoordinator
from research.performance import StrategyPerformanceStore


class FakeHistory:
    def __init__(self, bars):
        self._bars = bars
        self.calls = 0

    async def bars(self, contract, duration="365 D", bar_size="1 hour"):
        self.calls += 1
        return self._bars


def make_bars(count: int = 360) -> list[PriceBar]:
    price = 100.0
    bars = []
    for i in range(count):
        close = price + 0.2
        bars.append(PriceBar(str(i), price, close + 0.3, price - 0.3, close, 1000 + i))
        price = close
    return bars


def test_research_coordinator_refreshes_then_skips_fresh_context(tmp_path: Path):
    async def scenario() -> None:
        store = StrategyPerformanceStore(tmp_path / "perf.db")
        history = FakeHistory(make_bars())
        coordinator = ContinuousResearchCoordinator(history, store, freshness_hours=24)
        contract = SimpleNamespace(symbol="TEST", secType="STK")

        first = await coordinator.research_contract(contract)
        assert first.status == "REFRESHED"
        assert first.bars == 360
        assert store.research_status(symbol="TEST", asset_class="STK", timeframe="1 hour") is not None

        second = await coordinator.research_contract(contract)
        assert second.status == "SKIPPED"
        assert second.reason == "research_fresh"
        assert history.calls == 1

    asyncio.run(scenario())


def test_research_coordinator_rejects_insufficient_history(tmp_path: Path):
    async def scenario() -> None:
        store = StrategyPerformanceStore(tmp_path / "perf.db")
        history = FakeHistory(make_bars(200))
        coordinator = ContinuousResearchCoordinator(history, store)
        contract = SimpleNamespace(symbol="SHORT", secType="STK")

        result = await coordinator.research_contract(contract)
        assert result.status == "SKIPPED"
        assert result.reason == "insufficient_history"
        assert store.research_status(symbol="SHORT", asset_class="STK", timeframe="1 hour") is None

    asyncio.run(scenario())
