"""Replay uses the same decision path as runtime; execution semantics mirror the executor."""
import asyncio
from datetime import datetime, timedelta, timezone
from random import Random
from types import SimpleNamespace

from backtest.costs import CostModel
from config.config import RiskConfig
from engine.decision import DecisionPipeline
from market.history import PriceBar
from portfolio.brain import PortfolioSnapshot
from portfolio.state import PortfolioState
from research.pipeline_backtest import PipelineBacktest, PipelineConfig
from research.shadow_journal import ShadowJournal
from research.universe_provider import JournalUniverse, PointInTimeCsvUniverse, StaticCohortUniverse
from risk.risk_manager import RiskManager
from strategies.momentum import SignalSide, StrategySignal

T0 = datetime(2024, 1, 2, 10)


def walk(seed, n=300, drift=0.0005):
    rng = Random(seed)
    bars, price = [], 100.0
    for i in range(n):
        o = price
        c = max(1.0, o * (1 + rng.gauss(drift, 0.01)))
        bars.append(PriceBar(T0 + timedelta(hours=i), o, max(o, c) * 1.003, min(o, c) * 0.997, c, 1e6))
        price = c
    return bars


class AlwaysLong:
    name = "always_long_v1"
    warmup = 30

    def evaluate(self, bars):
        if len(bars) < self.warmup:
            return StrategySignal(SignalSide.FLAT, 0, None, None, None, "warmup")
        p = bars[-1].close
        return StrategySignal(SignalSide.LONG, 99, p, p * 0.97, p * 1.03, "always")


def replay(data, universe=None, **cfg):
    cfg.setdefault("cost_model", CostModel.zero())
    cfg.setdefault("pause_high_volatility", False)
    bt = PipelineBacktest(data, PipelineConfig(**cfg), universe=universe)
    bt.selector.strategies = [AlwaysLong()]
    return bt


def test_replay_and_runtime_share_the_decision_pipeline(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from engine.shadow import ShadowTradingEngine

    shadow = ShadowTradingEngine(SimpleNamespace(), SimpleNamespace(), risk_manager=RiskManager(RiskConfig()))
    bt = replay({"A": walk(1)})
    assert type(shadow.decision_pipeline) is DecisionPipeline
    assert type(bt.pipeline) is DecisionPipeline
    # Same selector + allocator + admission semantics -> identical decision on identical inputs.
    shadow.selector.strategies = [AlwaysLong()]
    bars = walk(3, 200)
    snap = PortfolioSnapshot(100_000, 100_000, 0, 0, 0, 0, 10_000)
    state = PortfolioState(snap, (), ())
    a = shadow.decision_pipeline.decide(bars, symbol="A", portfolio_state=state)
    b = bt.pipeline.decide(bars, symbol="A", portfolio_state=state)
    assert (a.action, a.reason, a.proposal.quantity) == (b.action, b.reason, b.proposal.quantity)
    assert a.approved


def test_runtime_shadow_decides_through_the_shared_pipeline(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from engine.shadow import ShadowTradingEngine

    calls = []
    candidate = SimpleNamespace(symbol="A", score=90.0, eligible=True,
                                contract=SimpleNamespace(secType="STK", conId=1, symbol="A"))

    async def ranked(rows_per_plan, quote_budget):
        return [candidate]

    intel = SimpleNamespace(ranked_us_opportunity_universe=ranked, last_funnel=[], universe=None, market_data=None)
    shadow = ShadowTradingEngine(SimpleNamespace(), intel, research_budget=0)

    async def fake_bars(contract, **kwargs):
        return walk(5, 120)

    shadow.history.bars = fake_bars
    shadow.session_policy = SimpleNamespace(state=lambda now=None: SimpleNamespace(
        market_open=False, session="CLOSED", local_time=datetime(2026, 1, 5, 20)))
    original = shadow.decision_pipeline.decide
    shadow.decision_pipeline.decide = lambda *a, **k: calls.append(k["symbol"]) or original(*a, **k)
    asyncio.run(shadow.run_once(5))
    assert calls == ["A"]


def test_limit_entry_fills_only_if_price_trades_through_and_expires_same_day():
    # Price jumps up right after every signal: a limit at the signal close never fills.
    bars = []
    price = 100.0
    for i in range(200):
        bars.append(PriceBar(T0 + timedelta(hours=i), price * 1.01, price * 1.02, price * 1.005, price * 1.015, 1e6))
        price *= 1.015
    result = replay({"A": bars}).run()
    assert result.trades == 0 and result.submitted > 0 and result.unfilled_expired == result.submitted
    legacy = replay({"A": bars}, entry_mode="next_open").run()
    assert legacy.trades > 0  # the old v1 assumption would have filled every one of them


def test_daily_entry_cap_matches_executor_default():
    data = {f"S{i}": walk(20 + i) for i in range(12)}
    result = replay(data, max_correlation=1.0).run()
    per_day = {}
    for t in result.trade_log:
        per_day[t.entry_time.date()] = per_day.get(t.entry_time.date(), 0) + 1
    assert result.submitted > 0 and max(per_day.values()) <= 3
    assert result.blocked_by_cap_or_lock > 0


def test_universe_provider_restricts_decisions():
    data = {"A": walk(1), "B": walk(2)}
    only_a = replay(data, universe=StaticCohortUniverse({"A"})).run()
    assert only_a.outside_universe > 0 and all(t.symbol == "A" for t in only_a.trade_log)
    assert StaticCohortUniverse({"A"}).survivorship_biased is True


def test_journal_universe_is_point_in_time_and_fails_closed(tmp_path):
    db = tmp_path / "j.db"
    j = ShadowJournal(db)
    import sqlite3
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO discovery_cycles (cycle_id, created_at) VALUES ('c1', '2026-03-02T15:00:00+00:00')")
        conn.execute("INSERT INTO discovery_cycles (cycle_id, created_at) VALUES ('c2', '2026-03-03T15:00:00+00:00')")
        for cycle, sym, status in [("c1", "OLD", "ranked_eligible"), ("c1", "WIDE", "ranked_rejected"),
                                   ("c2", "NEW", "ranked_eligible")]:
            conn.execute("INSERT INTO discovery_funnel (cycle_id, created_at, symbol, status) VALUES (?, '', ?, ?)",
                         (cycle, sym, status))
    u = JournalUniverse(db)
    assert u.survivorship_biased is False
    assert u.members_at(datetime(2026, 3, 2, 12, 0)) == {"OLD"}          # 12:00 NY = 17:00 UTC, after c1
    assert u.members_at(datetime(2026, 3, 3, 11, 0)) == {"NEW"}          # after c2
    assert u.members_at(datetime(2026, 3, 1, 12, 0)) == frozenset()      # before any cycle
    assert u.members_at(datetime(2026, 3, 9, 12, 0)) == frozenset()      # stale snapshot -> nobody


def test_point_in_time_csv_honours_delisting(tmp_path):
    path = tmp_path / "pit.csv"
    path.write_text("date,symbol,identifier,in_universe,delisted_on\n"
                    "2020-01-01,AAA,1,1,\n2020-01-01,BBB,2,1,2020-06-01\n"
                    "2020-07-01,AAA,1,1,\n2020-07-01,BBB,2,1,2020-06-01\n", encoding="utf-8")
    u = PointInTimeCsvUniverse(path)
    assert u.members_at(datetime(2020, 3, 1)) == {"AAA", "BBB"}
    assert u.members_at(datetime(2020, 8, 1)) == {"AAA"}
    assert u.members_at(datetime(2019, 1, 1)) == frozenset()
