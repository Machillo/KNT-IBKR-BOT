"""Order-free shadow execution model (shadow-only): executor-equivalent pure checks, virtual book,
frozen learning and journal v2 — with no executor object at all."""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from config.config import RiskConfig
from risk.risk_manager import RiskManager
from strategies.momentum import SignalSide
from test_shadow_submit_gate import Fixed, bars, state


def engine(tmp_path, *, market_open=True, data_type=1, n_candidates=1, bid=None, fail_symbol=None,
           learning_enabled=False, locked=False, bar_age=timedelta(0)):
    from engine.shadow import ShadowTradingEngine

    symbols = (("A", 1), ("B", 2), ("C", 3), ("D", 4), ("E", 5))[:n_candidates]
    candidates = [SimpleNamespace(symbol=sym, score=90.0, eligible=True, scanner_rank=cid, reference_price=100.0,
                                  spread_bps=1.0, reason="ok",
                                  contract=SimpleNamespace(secType="STK", conId=cid, symbol=sym))
                  for sym, cid in symbols]

    async def ranked(rows_per_plan, quote_budget):
        return candidates

    last_close = {}

    async def snap(contract, symbol, timeout=3.0):
        px = bid if bid is not None else last_close[symbol]  # default: a live quote at the last close
        return SimpleNamespace(bid=px, ask=px + 0.01, last=px, market_price=px, market_data_type=data_type)

    intel = SimpleNamespace(ranked_us_opportunity_universe=ranked, last_funnel=[], universe=None,
                            market_data=SimpleNamespace(snapshot_contract=snap,
                                                        settings=SimpleNamespace(market_data_type=1)))

    async def details(contract):
        return [SimpleNamespace(industry=f"Industry{contract.conId}", category="x")]

    risk = RiskManager(RiskConfig())
    if locked:
        risk.lock_trading("test")
    shadow = ShadowTradingEngine(SimpleNamespace(reqContractDetailsAsync=details), intel, research_budget=0,
                                 risk_manager=risk, paper_executor=None, state_dir=tmp_path / "state",
                                 learning_enabled=learning_enabled, research_execution=True,
                                 run_mode="shadow_only", config_hash="cfg-test")
    shadow.selector.strategies = [Fixed(SignalSide.LONG)]
    shadow.selector.pause_directional_high_volatility = False
    series = bars()

    async def fake_bars(contract, **kwargs):
        if fail_symbol and getattr(contract, "symbol", None) == fail_symbol:
            raise TimeoutError("history timeout")
        # Different return paths per symbol so correlation never blocks in these tests.
        from random import Random
        rng = Random(int(getattr(contract, "conId", 1) or 1))
        # tz-aware hourly bars whose LAST bar completed 10 minutes ago (fresh decision bar)
        last_start = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=70) - bar_age
        n = len(series)
        out = [b.__class__(last_start - timedelta(hours=n - 1 - i), b.open, b.high, b.low,
                           b.close * (1 + rng.gauss(0, 0.004)), b.volume)
               for i, b in enumerate(series)]
        last_close[contract.symbol] = out[-1].close
        return out

    shadow.history.bars = fake_bars
    shadow.session_policy = SimpleNamespace(state=lambda now=None: SimpleNamespace(
        market_open=market_open, session="REGULAR" if market_open else "CLOSED", local_time=datetime(2026, 1, 5, 11)))
    return shadow


def run(shadow, portfolio=None):
    return asyncio.run(shadow.run_once(5, portfolio_state=portfolio if portfolio is not None else state()))


def rows(shadow, sql="SELECT * FROM shadow_decisions ORDER BY id"):
    conn = sqlite3.connect(shadow.journal.path)
    conn.row_factory = sqlite3.Row
    return conn.execute(sql).fetchall()


def test_approved_setup_is_journaled_as_shadow_submit_with_context(tmp_path):
    shadow = engine(tmp_path)
    decisions = run(shadow)
    assert decisions[0].action == "SHADOW_SUBMIT"
    r = rows(shadow)[0]
    assert r["run_mode"] == "shadow_only" and r["learning_mode"] == "frozen"
    assert r["quantity"] >= 1 and r["notional"] > 0 and r["risk_amount"] > 0
    assert r["reference_data_type"] == 1 and r["session_open"] == 1 and r["sector"] == "Industry1"
    assert r["bar_count"] == 120 and r["decision_version"].endswith("pipeline_v2")
    cycle = rows(shadow, "SELECT * FROM discovery_cycles")[0]
    assert cycle["cycle_end"] and cycle["candidates_attempted"] == 1 and cycle["candidate_errors"] == 0


def test_closed_session_is_blocked_like_the_executor(tmp_path):
    decisions = run(engine(tmp_path, market_open=False))
    assert (decisions[0].action, decisions[0].portfolio_reason) == ("SHADOW_BLOCKED", "market_session_closed")


def test_delayed_quote_is_blocked_like_the_executor(tmp_path):
    decisions = run(engine(tmp_path, data_type=3))
    assert (decisions[0].action, decisions[0].portfolio_reason) == ("SHADOW_BLOCKED", "reference_not_live_market_data")


def test_far_reference_is_blocked_like_the_executor(tmp_path):
    decisions = run(engine(tmp_path, bid=10.0))
    assert decisions[0].portfolio_reason == "entry_far_from_fresh_reference"


def test_daily_cap_and_virtual_book_carry_across_cycles(tmp_path):
    shadow = engine(tmp_path, n_candidates=5)
    first = run(shadow)
    submitted = [d for d in first if d.action == "SHADOW_SUBMIT"]
    assert len(submitted) == 3                                          # executor default cap
    assert {d.portfolio_reason for d in first if d.action == "SHADOW_BLOCKED"} == {"daily_entry_limit_reached"}
    # Next cycle, same day, same (unchanged broker) state: today's would-be entries are
    # pending exposure, so the same names are duplicates and the cap still binds.
    second = run(shadow)
    reasons = {d.portfolio_reason for d in second}
    assert "all_pre_trade_checks_passed" not in reasons


def test_learning_is_frozen_in_shadow_only(tmp_path):
    shadow = engine(tmp_path)
    assert shadow.selector.learning_engine is None
    called = []

    async def scheduler(*a, **k):
        called.append(1)
    shadow.research_scheduler.run_cycle = scheduler
    run(shadow)
    assert called == []
    assert rows(shadow)[0]["selector_bonus"] == 0.0


def test_failed_candidate_is_journaled_not_dropped(tmp_path):
    shadow = engine(tmp_path, n_candidates=2, fail_symbol="B")
    run(shadow)
    actions = {r["symbol"]: (r["action"], r["reason"]) for r in rows(shadow)}
    assert actions["B"] == ("CANDIDATE_ERROR", "TimeoutError")
    cycle = rows(shadow, "SELECT * FROM discovery_cycles")[0]
    assert (cycle["candidates_attempted"], cycle["candidate_errors"]) == (2, 1)


def test_research_lock_blocks_before_pretrade(tmp_path):
    decisions = run(engine(tmp_path, locked=True))
    assert decisions[0].action == "PORTFOLIO_REJECTED"


def test_duplicates_are_per_run_mode_and_conflicts_flagged(tmp_path):
    from research.shadow_journal import ShadowJournal

    j = ShadowJournal(tmp_path / "j.db")
    base = dict(con_id=7, bar_time="2026-01-05T15:00:00+00:00", timeframe="1 hour", regime="TREND",
                score=90.0, entry=1.0, stop=0.9, target=1.2, reason="x")
    a = j.record_decision("c1", symbol="X", action="SHADOW_SUBMIT", strategy="s1", side="LONG",
                          context={"run_mode": "shadow_only", "input_hash": "h1"}, **base)
    b = j.record_decision("c2", symbol="X", action="PAPER_SUBMITTED", strategy="s1", side="LONG",
                          context={"run_mode": "runtime", "input_hash": "h1"}, **base)
    c = j.record_decision("c3", symbol="X", action="NO_TRADE", strategy=None, side=None,
                          context={"run_mode": "shadow_only", "input_hash": "h1"}, **base)
    d = j.record_decision("c4", symbol="X", action="NO_TRADE", strategy=None, side=None,
                          context={"run_mode": "shadow_only", "input_hash": "revised"}, **base)
    conn = sqlite3.connect(j.path)
    got = {i: conn.execute("SELECT duplicate_of, conflict_with FROM shadow_decisions WHERE id=?", (i,)).fetchone()
           for i in (a, b, c, d)}
    assert got[a] == (None, None) and got[b] == (None, None)            # other run mode: not a duplicate
    assert got[c] == (a, a)                                              # same bar AND inputs, different output
    assert got[d] == (a, None)                                           # IBKR revised the bar: not non-determinism


def test_virtual_entry_is_pending_exposure_in_the_next_cycle(tmp_path):
    shadow = engine(tmp_path)
    shadow.max_entries_per_day = 10  # the cap must not be what blocks here
    assert run(shadow)[0].action == "SHADOW_SUBMIT"
    again = run(shadow)[0]
    # Seen as exposure (like a real working order): admission refuses it against itself.
    assert again.action != "SHADOW_SUBMIT"
    assert again.portfolio_reason in {"correlation_limit", "duplicate_symbol_exposure"}



def test_yesterdays_last_bar_is_not_traded_at_the_open(tmp_path):
    decisions = run(engine(tmp_path, bar_age=timedelta(hours=17)))
    assert (decisions[0].action, decisions[0].portfolio_reason) == ("SHADOW_BLOCKED", "stale_decision_bar")
