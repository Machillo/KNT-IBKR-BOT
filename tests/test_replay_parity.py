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
    a = shadow.decision_pipeline.decide(bars, symbol="A", portfolio_state=state, sector="Technology")
    b = bt.pipeline.decide(bars, symbol="A", portfolio_state=state, sector="Technology")
    # Documented difference: runtime requires sector metadata (the cache has none).
    assert shadow.admission.require_sector_metadata is True and bt.admission.require_sector_metadata is False
    assert shadow.decision_pipeline.decide(bars, symbol="A", portfolio_state=state).reason == "sector_metadata_missing"
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
    assert result.trades == 0 and result.submitted > 0
    assert result.unfilled_expired + result.censored_at_end == result.submitted
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
                    "2020-07-01,AAA,1,1,\n2020-07-01,BBB,2,0,2020-06-01\n", encoding="utf-8")
    u = PointInTimeCsvUniverse(path, max_age_days=365)
    assert u.members_at(datetime(2020, 1, 1, 12)) == frozenset()          # same-day snapshot not yet effective
    assert u.members_at(datetime(2020, 3, 1)) == {"AAA", "BBB"}
    assert u.members_at(datetime(2020, 6, 15)) == {"AAA"}                  # delisting enforced against t
    assert u.members_at(datetime(2020, 8, 1)) == {"AAA"}
    assert u.members_at(datetime(2019, 1, 1)) == frozenset()
    assert PointInTimeCsvUniverse(path, max_age_days=30).members_at(datetime(2020, 12, 1)) == frozenset()


def test_journal_universe_empty_cycle_fails_closed(tmp_path):
    import sqlite3
    db = tmp_path / "j.db"
    ShadowJournal(db)
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO discovery_cycles (cycle_id, created_at) VALUES ('c1', '2026-03-02T15:00:00+00:00')")
        conn.execute("INSERT INTO discovery_cycles (cycle_id, created_at) VALUES ('c2', '2026-03-02T16:00:00+00:00')")
        conn.execute("INSERT INTO discovery_funnel (cycle_id, created_at, symbol, status) VALUES ('c1', '', 'OLD', 'ranked_eligible')")
    assert JournalUniverse(db).members_at(datetime(2026, 3, 2, 12, 30)) == frozenset()  # c2 empty replaces c1


def test_replay_correlation_inputs_are_aligned_not_lagged():
    data = {"A": walk(1), "B": walk(2)}
    bt = replay(data)
    t = bt.times["A"][150]
    through = bt._bars_through("B", t, 61)
    assert through[-1].time == data["B"][150].time        # includes the bar completed at t
    assert bt._bars_until("B", t, 61)[-1].time == data["B"][149].time


def test_identical_series_are_rejected_by_the_correlation_guard():
    base = walk(7)
    twin = [PriceBar(b.time, b.open * 2, b.high * 2, b.low * 2, b.close * 2, b.volume) for b in base]
    result = replay({"A": base, "B": twin}).run()
    # A and B move identically: once one is held, the other must be rejected (corr 1.0 > 0.85).
    overlaps = [(x, y) for x in result.trade_log for y in result.trade_log
                if x.symbol != y.symbol and x.entry_time < y.exit_time and y.entry_time < x.exit_time]
    assert overlaps == []
    assert result.rejected_by_portfolio > 0


class Scored:
    warmup = 30

    def __init__(self, name, score):
        self.name, self.score = name, score

    def evaluate(self, bars):
        p = bars[-1].close
        return StrategySignal(SignalSide.LONG, self.score, p, p * 0.97, p * 1.03, self.name)


def test_capacity_goes_in_the_runtime_liquidity_order_not_by_score():
    """The runtime meets candidates in liquidity order and the first approved one takes the
    daily cap; the replay must do the same (it used to favour the highest selector score)."""
    rng = Random(3)
    data = {name: walk(rng.randint(0, 999)) for name in ("AAA", "ZZZ")}
    # ZZZ trades 10x the dollar volume of AAA -> more liquid -> met first.
    data["ZZZ"] = [PriceBar(b.time, b.open, b.high, b.low, b.close, b.volume * 10) for b in data["ZZZ"]]
    bt = PipelineBacktest(data, PipelineConfig(cost_model=CostModel.zero(), pause_high_volatility=False,
                                               max_entries_per_day=1, max_correlation=1.0))

    class AAAScoresHigher:
        name, warmup = "aaa_scores_higher", 30

        def evaluate(self, bars):
            p = bars[-1].close
            score = 90 if abs(p - data["AAA"][len(bars) - 1].close) < 1e-9 else 60
            return StrategySignal(SignalSide.LONG, score, p, p * 0.97, p * 1.03, "s")

    bt.selector.strategies = [AAAScoresHigher()]
    result = bt.run()
    first = min(result.trade_log, key=lambda x: (x.entry_time, x.symbol))
    assert first.symbol == "ZZZ"


def test_intraday_orders_decided_after_the_session_close_are_refused():
    bars = []
    start = datetime(2024, 1, 2)
    for d in range(30):                                   # > DECISION_CONTEXT_BARS of history
        for h in range(7):
            p = 100 + d + h * 0.1
            bars.append(PriceBar(start + timedelta(days=d, hours=10 + h), p, p + 0.5, p - 0.5, p, 1e6))

    class LastBarOnly:
        name, warmup = "last_bar_only", 30

        def evaluate(self, b):
            p = b[-1].close
            if b[-1].time.hour != 16:
                return StrategySignal(SignalSide.FLAT, 0, None, None, None, "flat")
            return StrategySignal(SignalSide.LONG, 99, p, p * 0.97, p * 1.03, "close")

    bt = replay({"A": bars})
    bt.selector.strategies = [LastBarOnly()]
    result = bt.run()
    assert result.blocked_session_closed > 0 and result.submitted == 0


def test_working_orders_count_for_the_correlation_guard_and_fail_closed():
    from portfolio.admission import PortfolioAdmissionCoordinator
    from portfolio.allocation import PortfolioAllocator
    from portfolio.state import PendingOrderExposure
    from engine.strategy_selector import StrategySelector

    selector = StrategySelector(performance_store=None, pause_directional_high_volatility=False)
    selector.strategies = [AlwaysLong()]
    risk = RiskManager(RiskConfig())
    pipeline = DecisionPipeline(selector, PortfolioAllocator(), PortfolioAdmissionCoordinator(risk))
    bars = walk(9, 200)
    pending = (PendingOrderExposure("B", "STK", 10, 100.0, 1_000.0, con_id=2),)
    state = PortfolioState(PortfolioSnapshot(100_000, 100_000, 0, 0, 1_000, 0, 10_000), (), pending)
    missing = pipeline.decide(bars, symbol="A", portfolio_state=state, position_returns={})
    assert missing.action == "PORTFOLIO_REJECTED" and missing.reason == "correlation_unavailable"
    from engine.decision import return_series
    same = pipeline.decide(bars, symbol="A", portfolio_state=state, position_returns={"B": return_series(bars)})
    assert same.action == "PORTFOLIO_REJECTED" and same.reason == "correlation_limit"


def test_journal_contract_listing_and_file_loader(tmp_path):
    import json
    import sqlite3
    from run_fetch_journal_bars import journal_contracts
    from run_pipeline_backtest import load

    db = tmp_path / "j.db"
    ShadowJournal(db)
    with sqlite3.connect(db) as conn:
        for sym, cid, status in [("NEW", 7, "ranked_eligible"), ("NEW", 7, "ranked_eligible"),
                                 ("WIDE", 8, "ranked_rejected"), ("ZERO", 0, "ranked_eligible")]:
            conn.execute("INSERT INTO discovery_funnel (cycle_id, created_at, symbol, con_id, status) "
                         "VALUES ('c', '', ?, ?, ?)", (sym, cid, status))
    assert journal_contracts(str(db)) == [("NEW", 7)]
    (tmp_path / "NEW_intraday_1y.json").write_text(json.dumps(
        [{"time": "2026-01-05T10:00:00-05:00", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}]), encoding="utf-8")
    assert list(load("files", "intraday_1y", str(tmp_path))) == ["NEW"]


def test_replay_time_key_converts_utc_bars_to_exchange_time():
    """Regression: bars fetched with complete_only carry UTC offsets; stripping without
    converting made a 14:30Z bar look like 14:30 ET (= 18:30Z), so journal-universe snapshots
    taken up to 4-5 h LATER decided membership (look-ahead)."""
    from research.pipeline_backtest import _key

    assert _key("2026-03-02T14:30:00+00:00") == datetime(2026, 3, 2, 9, 30)      # EST: -5 h
    assert _key("2026-07-01T13:30:00+00:00") == datetime(2026, 7, 1, 9, 30)      # EDT: -4 h
    assert _key("2026-03-02T09:30:00-05:00") == datetime(2026, 3, 2, 9, 30)      # already ET
    assert _key("2026-03-02") == datetime(2026, 3, 2)                            # daily: unchanged


def test_journal_universe_does_not_see_later_snapshots_through_utc_bars(tmp_path):
    import sqlite3

    from research.pipeline_backtest import _key
    db = tmp_path / "j.db"
    ShadowJournal(db)
    with sqlite3.connect(db) as conn:
        # Snapshot taken at 17:00Z = 12:00 ET.
        conn.execute("INSERT INTO discovery_cycles (cycle_id, created_at) VALUES ('c1', '2026-03-02T17:00:00+00:00')")
        conn.execute("INSERT INTO discovery_funnel (cycle_id, created_at, symbol, status) VALUES ('c1', '', 'AAA', 'ranked_eligible')")
    u = JournalUniverse(db)
    # A 14:30Z (09:30 ET) bar is BEFORE the snapshot: nobody is a member yet.
    assert u.members_at(_key("2026-03-02T14:30:00+00:00")) == frozenset()
    assert u.members_at(_key("2026-03-02T17:30:00+00:00")) == {"AAA"}


def test_fetch_keeps_older_history_and_separates_reused_tickers(tmp_path):
    from run_fetch_journal_bars import cache_file, merge_bars

    old = [{"time": "2026-01-02T15:00:00+00:00", "close": 1}, {"time": "2026-01-02T16:00:00+00:00", "close": 2}]
    new = [{"time": "2026-01-02T16:00:00+00:00", "close": 2.5}, {"time": "2026-06-01T15:00:00+00:00", "close": 3}]
    merged = merge_bars(old, new)
    assert [r["close"] for r in merged] == [1, 2.5, 3]           # oldest bar kept, overlap refreshed
    contracts = [("ABC", 1), ("ABC", 2), ("XYZ", 3)]
    assert cache_file(tmp_path, "ABC", 2, contracts).name == "ABC.2_intraday_1y.json"
    assert cache_file(tmp_path, "XYZ", 3, contracts).name == "XYZ_intraday_1y.json"


def test_ibkr_score_provider_requests_each_contract_once():
    import asyncio
    from types import SimpleNamespace

    from run_score_shadow import IBKRHistoryBarsProvider

    calls = []

    async def bars(contract, **kw):
        calls.append(contract.conId)
        return [SimpleNamespace(time=datetime(2026, 1, 5, 15 + i)) for i in range(5)]

    provider = IBKRHistoryBarsProvider(SimpleNamespace(), min_interval_seconds=0)
    provider.history = SimpleNamespace(bars=bars)
    for at in ("2026-01-05T15:00:00", "2026-01-05T16:00:00", "2026-01-05T17:00:00"):
        asyncio.run(provider.bars_from("A", 1, at, "1 hour"))
    assert calls == [1]


# ---- v4 parity: shared context, executor pre-trade, drawdown lock, config from .env ----

class _Recording(AlwaysLong):
    def __init__(self):
        self.lengths = []

    def evaluate(self, bars):
        self.lengths.append(len(bars))
        return super().evaluate(bars)


def test_replay_and_runtime_see_the_same_number_of_context_bars(tmp_path):
    from engine.decision import DECISION_CONTEXT_BARS
    from test_shadow_research_path import engine as shadow_engine
    from test_shadow_research_path import run as shadow_run

    bt = replay({"A": walk(1, n=400)})
    rec = _Recording()
    bt.selector.strategies = [rec]
    bt.run()
    assert max(rec.lengths) == DECISION_CONTEXT_BARS

    shadow = shadow_engine(tmp_path)
    live = _Recording()
    shadow.selector.strategies = [live]

    async def long_history(contract, **kw):
        assert kw.get("duration") == "45 D"
        return walk(2, n=300)
    shadow.history.bars = long_history
    shadow_run(shadow)
    assert live.lengths == [DECISION_CONTEXT_BARS]


def test_replay_applies_the_executor_pretrade_checks_on_rounded_prices():
    class SubTick(AlwaysLong):
        def evaluate(self, bars):
            p = round(bars[-1].close, 2)
            # stop 0.004 below entry: rounds to the entry -> invalid geometry after normalization
            return StrategySignal(SignalSide.LONG, 99, p, p - 0.004, p * 1.03, "subtick")
    bt = replay({"A": walk(3)})
    bt.selector.strategies = [SubTick()]
    result = bt.run()
    assert result.submitted == 0 and result.blocked_by_pretrade > 0


def test_replay_drawdown_lock_is_sticky_across_days():
    # Steady decline: the multi-day drawdown lock must stop new entries and never reset.
    down = []
    price = 100.0
    for i in range(600):
        price *= 0.9985
        down.append(PriceBar(T0 + timedelta(hours=i), price * 1.001, price * 1.004, price * 0.996, price, 1e6))
    locked = replay({"A": down}, max_drawdown_pct=0.02, daily_loss_pct=0.5).run()
    free = replay({"A": down}, max_drawdown_pct=0.5, daily_loss_pct=0.5).run()
    assert locked.submitted < free.submitted
    # The lock reaches admission first (snapshot.trading_locked), as at runtime.
    assert locked.rejected_by_portfolio > free.rejected_by_portfolio


def test_replay_config_follows_the_runtime_env():
    from config.config import BotConfig, IBKRConfig, RiskConfig, RuntimeConfig

    bot = BotConfig(ibkr=IBKRConfig(), runtime=RuntimeConfig(),
                    risk=RiskConfig(max_trade_risk_pct=0.005, max_daily_loss_pct=0.02, max_position_pct=0.05,
                                    max_drawdown_pct=0.08))
    cfg = PipelineConfig.from_bot_config(bot)
    assert (cfg.max_trade_risk_pct, cfg.daily_loss_pct, cfg.max_position_pct, cfg.max_drawdown_pct) == \
        (0.005, 0.02, 0.05, 0.08)
    assert cfg.risk_pct == 0.005 and cfg.runtime_equivalent


def test_research_variants_are_flagged_as_not_runtime_equivalent():
    assert PipelineConfig().runtime_equivalent is True
    for change in (dict(regime_filter=("TRENDING",)), dict(symbol_trend_sma=50), dict(bracket_atr=(1.0, 2.0)),
                   dict(use_volatility_multiplier=False), dict(entry_mode="next_open"), dict(context_bars=450)):
        assert PipelineConfig(**change).runtime_equivalent is False
    assert replay({"A": walk(1)}, regime_filter=("TRENDING",)).run().runtime_equivalent is False


def test_journal_universe_top_n_matches_the_runtime_deep_analysis_cap(tmp_path):
    import sqlite3
    db = tmp_path / "j.db"
    ShadowJournal(db)
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO discovery_cycles (cycle_id, created_at) VALUES ('c1', '2026-03-02T15:00:00+00:00')")
        for sym, score, rank in [("LOW", 10.0, 1), ("HIGH", 90.0, 5), ("MID", 50.0, 2), ("TIE", 50.0, 1)]:
            conn.execute("INSERT INTO discovery_funnel (cycle_id, created_at, symbol, status, liquidity_score, best_rank) "
                         "VALUES ('c1', '', ?, 'ranked_eligible', ?, ?)", (sym, score, rank))
    at = datetime(2026, 3, 2, 11, 0)
    assert JournalUniverse(db).members_at(at) == {"LOW", "HIGH", "MID", "TIE"}
    assert JournalUniverse(db, top_n=2).members_at(at) == {"HIGH", "TIE"}   # score desc, then scanner rank


def _pit(tmp_path, body, header="date,symbol,identifier,in_universe,delisted_on"):
    path = tmp_path / "pit.csv"
    path.write_text(header + "\n" + body, encoding="utf-8")
    return path


def test_point_in_time_loader_fails_closed_on_bad_data(tmp_path):
    import pytest
    cases = [
        ("2020-01-01,AAA,1\n", "date,symbol,identifier"),                      # missing column
        ("2020-01-01,AAA,,1,\n", None),                                        # empty identifier
        ("2020-07-01,BBB,2,1,2020-06-01\n", None),                             # member after its delisting
        ("2020-01-01,ABC,1,1,\n2020-01-01,ABC,9,1,\n", None),                  # ambiguous ticker
    ]
    for body, header in cases:
        path = _pit(tmp_path, body, header) if header else _pit(tmp_path, body)
        with pytest.raises(ValueError):
            PointInTimeCsvUniverse(path)


def test_point_in_time_loader_follows_ticker_changes_and_exchange_time(tmp_path):
    # Identifier 7 was OLDCO until 2020-03-31, NEWCO afterwards.
    path = _pit(tmp_path, "2020-03-31,OLDCO,7,1,\n2020-04-30,NEWCO,7,1,\n")
    u = PointInTimeCsvUniverse(path, max_age_days=60)
    assert u.members_at(datetime(2020, 4, 15, 10)) == {"OLDCO"}
    assert u.members_at(datetime(2020, 5, 5, 10)) == {"NEWCO"}
    # Aware UTC query converted to exchange time: 2020-05-01 03:00Z = 2020-04-30 23:00 ET (before the lag).
    assert u.members_at(datetime(2020, 5, 1, 3, tzinfo=timezone.utc)) == {"OLDCO"}


def test_point_in_time_sectors_fail_closed_when_unknown_or_stale(tmp_path):
    from research.universe_provider import PointInTimeSectors
    path = tmp_path / "sectors.csv"
    path.write_text("date,symbol,identifier,sector\n2020-01-01,AAA,1,Technology\n2020-06-01,AAA,1,Energy\n",
                    encoding="utf-8")
    s = PointInTimeSectors(path, max_age_days=100)
    assert s.sector_at("AAA", datetime(2020, 1, 1, 12)) is None          # same-day row not yet effective
    assert s.sector_at("AAA", datetime(2020, 3, 1)) == "Technology"
    assert s.sector_at("AAA", datetime(2020, 7, 1)) == "Energy"
    assert s.sector_at("AAA", datetime(2021, 7, 1)) is None              # stale
    assert s.sector_at("ZZZ", datetime(2020, 7, 1)) is None              # unknown



def test_replay_skips_decisions_without_the_full_runtime_context_and_flags_bar_size():
    short = replay({"A": walk(1, n=100)}).run()
    assert short.submitted == 0 and short.warmup_skipped > 0
    hourly = replay({"A": walk(1, n=300)}).run()
    assert hourly.hourly_data is True and hourly.reference_unchecked == hourly.submitted
    daily = [PriceBar(T0 + timedelta(days=i), b.open, b.high, b.low, b.close, b.volume)
             for i, b in enumerate(walk(1, n=300))]
    assert replay({"A": daily}).run().runtime_equivalent is False     # the runtime never decides on daily bars


def test_every_decision_knob_breaks_runtime_equivalence():
    for change in (dict(min_score=70.0), dict(strategies=("x",)), dict(max_entries_per_day=5),
                   dict(max_correlation=0.9), dict(pause_high_volatility=False)):
        assert PipelineConfig(**change).runtime_equivalent is False, change



def test_merge_bars_dedupes_the_same_instant_written_differently():
    from run_fetch_journal_bars import merge_bars
    merged = merge_bars([{"time": "2026-01-02T15:00:00+00:00", "close": 1}],
                        [{"time": "2026-01-02T10:00:00-05:00", "close": 2}])
    assert [r["close"] for r in merged] == [2]
