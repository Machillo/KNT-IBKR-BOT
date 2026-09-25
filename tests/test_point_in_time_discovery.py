"""Point-in-time discovery funnel and shadow journaling (offline fakes, no IBKR)."""
import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from core.market_data import MarketSnapshot
from market.discovery import IBKRDiscoveryService, ScannerPlan
from market.history import PriceBar
from market.intelligence import MarketIntelligenceService


def contract(con_id, symbol, sec_type="STK"):
    return SimpleNamespace(conId=con_id, symbol=symbol, localSymbol=symbol, secType=sec_type,
                           primaryExchange="NASDAQ", exchange="SMART", currency="USD")


class FakeScannerIB:
    """Each scanner returns a fixed list of (rank, contract)."""

    def __init__(self, by_code):
        self.by_code = by_code

    async def reqScannerDataAsync(self, sub):
        return [SimpleNamespace(rank=r, contractDetails=SimpleNamespace(contract=c))
                for r, c in self.by_code[sub.scanCode]]


class FakeMarketData:
    def __init__(self, quotes, fail=()):
        self.quotes, self.fail = quotes, set(fail)
        self.settings = SimpleNamespace(market_data_type=1)

    def configure(self):
        pass

    async def snapshot_contract(self, c, symbol, timeout=3.0):
        if symbol in self.fail:
            raise TimeoutError("no quote")
        bid, ask = self.quotes[symbol]
        return MarketSnapshot(symbol, bid, ask, (bid + ask) / 2, (bid + ask) / 2, 1e6, 1)


AAA, BBB, CCC, WRAP = contract(1, "AAA"), contract(2, "BBB"), contract(3, "CCC"), contract(4, "XX RT")
PLANS = [ScannerPlan("MOST_ACTIVE", "STK", "STK.US.MAJOR", "MOST_ACTIVE"),
         ScannerPlan("TOP_GAIN", "STK", "STK.US.MAJOR", "TOP_PERC_GAIN")]
IB = FakeScannerIB({"MOST_ACTIVE": [(0, AAA), (1, BBB), (2, WRAP)], "TOP_PERC_GAIN": [(0, CCC), (3, AAA)]})


def test_scan_many_keeps_every_scanner_source():
    merged = asyncio.run(IBKRDiscoveryService(IB).scan_many(PLANS, 5))
    aaa = next(c for c in merged if c.symbol == "AAA")
    assert aaa.rank == 0 and set(aaa.sources) == {("MOST_ACTIVE", 0), ("TOP_GAIN", 3)}


def test_funnel_records_every_discovered_contract_and_why():
    svc = MarketIntelligenceService(IB, FakeMarketData({"AAA": (10.0, 10.01), "BBB": (5.0, 5.5)}, fail={"CCC"}))
    asyncio.run(svc.ranked_candidates(PLANS, rows_per_plan=5, quote_budget=3))
    status = {r.symbol: r.status for r in svc.last_funnel}
    assert status == {"AAA": "ranked_eligible", "BBB": "ranked_rejected", "CCC": "quote_error",
                      "XX RT": "subtype_rejected"}
    bbb = next(r for r in svc.last_funnel if r.symbol == "BBB")
    assert bbb.spread_bps > 50 and "spread" in bbb.reason


def test_funnel_marks_candidates_beyond_quote_budget():
    svc = MarketIntelligenceService(IB, FakeMarketData({"AAA": (10.0, 10.01), "CCC": (3.0, 3.001)}))
    asyncio.run(svc.ranked_candidates(PLANS, rows_per_plan=5, quote_budget=2))
    assert {r.symbol: r.status for r in svc.last_funnel}["BBB"] == "not_quoted_budget"


def _bars(n=120):
    t0 = datetime(2026, 1, 5, 14, tzinfo=timezone.utc)
    out, price = [], 100.0
    for i in range(n):
        price *= 1.004
        out.append(PriceBar(t0 + timedelta(hours=i), price * 0.999, price * 1.003, price * 0.997, price, 1e6))
    return out


def test_shadow_cycle_journals_cycle_funnel_and_decision_context(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # every default state/ path lands in the temp dir
    from engine.shadow import ShadowTradingEngine

    intel = MarketIntelligenceService(IB, FakeMarketData({"AAA": (10.0, 10.01), "BBB": (5.0, 5.01), "CCC": (3.0, 3.001)}))
    shadow = ShadowTradingEngine(SimpleNamespace(), intel, research_budget=0)

    async def common_stock(candidate):
        return "COMMON", True               # instrument type lookup (read-only IBKR details) stubbed
    shadow._stock_type = common_stock
    bars = _bars()

    async def fake_bars(contract, **kwargs):
        return bars

    shadow.history.bars = fake_bars
    shadow.session_policy = SimpleNamespace(state=lambda now=None: SimpleNamespace(
        market_open=False, session="CLOSED", local_time=datetime(2026, 1, 5, 20)))

    async def fake_ranked(rows_per_plan, quote_budget):
        intel.universe = SimpleNamespace(name="TEST_UNIVERSE", scanners=PLANS)
        return await intel.ranked_candidates(PLANS, rows_per_plan, quote_budget)

    intel.ranked_us_opportunity_universe = fake_ranked
    decisions = asyncio.run(shadow.run_once(5))
    assert decisions
    with sqlite3.connect(tmp_path / "state" / "strategy_performance.db") as conn:
        cycle = conn.execute("SELECT universe, scanners, market_data_type FROM discovery_cycles").fetchone()
        funnel = conn.execute("SELECT COUNT(*) FROM discovery_funnel").fetchone()[0]
        rows = conn.execute("SELECT action, regime, top_strategy, selector_threshold, decision_version "
                            "FROM shadow_decisions").fetchall()
    assert cycle[0] == "TEST_UNIVERSE" and json.loads(cycle[1]) == ["MOST_ACTIVE", "TOP_GAIN"] and cycle[2] == 1
    assert funnel == 4
    assert len(rows) == len(decisions)
    assert all(r[3] == 55.0 and r[4] for r in rows)
    assert any(r[2] is not None for r in rows)  # best directional evaluation kept even if NO_TRADE
