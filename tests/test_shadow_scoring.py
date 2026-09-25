import asyncio
import hashlib
import sqlite3
from datetime import datetime, timedelta

import pytest

from backtest.costs import CostModel
from market.history import PriceBar
from research.shadow_journal import ShadowJournal, score_decision
from research.shadow_scoring import InMemoryBarsProvider, ShadowScorer

T0 = datetime(2026, 2, 2, 10)
DECIDED = T0 + timedelta(minutes=30)  # decision recorded before the next bar (T0+1h) starts
ZERO = CostModel.zero()


def bars(prices, start=1):
    """Hourly bars after T0; each (o, h, l, c)."""
    return [PriceBar(T0 + timedelta(hours=start + i), *p, 1000) for i, p in enumerate(prices)]


def journal_with_decisions(path):
    j = ShadowJournal(path)
    c = j.new_cycle_id()
    trade = j.record_decision(c, symbol="AAA", con_id=1, bar_time=T0, timeframe="1 hour", regime="TRENDING",
                              action="SHADOW_SUBMIT", strategy="breakout_v1", side="LONG", score=70.0,
                              entry=100.0, stop=95.0, target=110.0, reason="best",
                              context={"reference_close": 100.0}, created_at=DECIDED)
    no_trade = j.record_decision(c, symbol="BBB", con_id=2, bar_time=T0, timeframe="1 hour", regime="MIXED",
                                 action="NO_TRADE", strategy=None, side=None, score=None, entry=None, stop=None,
                                 target=None, reason="best_signal_below_threshold",
                                 top={"strategy": "range_v1", "side": "LONG", "score": 40.0, "entry": 50.0,
                                      "stop": 48.0, "target": 55.0},
                                 context={"reference_close": 50.0}, created_at=DECIDED)
    silent = j.record_decision(c, symbol="CCC", con_id=3, bar_time=T0, timeframe="1 hour", regime="RANGE",
                               action="NO_TRADE", strategy=None, side=None, score=None, entry=None, stop=None,
                               target=None, reason="all_strategies_flat", context={"reference_close": 20.0}, created_at=DECIDED)
    return j, trade, no_trade, silent


def decisions_digest(path):
    with sqlite3.connect(path) as conn:
        rows = conn.execute("SELECT * FROM shadow_decisions ORDER BY id").fetchall()
    return hashlib.sha256(repr(rows).encode()).hexdigest()


FLAT20 = [(100, 101, 99, 100)] * 25


def provider(extra_hit=True):
    aaa = [(100, 101, 99, 100), (100, 111, 99, 110)] + [(110, 111, 109, 110)] * 23
    bbb = [(50, 51, 49.5, 50), (50, 56, 49.9, 55)] + [(55, 56, 54, 55)] * 23
    ccc = [(20, 20.5, 19.5, 20 + i * 0.1) for i in range(25)]
    # Include bars AT and BEFORE the decision time that must be ignored.
    past = [PriceBar(T0 - timedelta(hours=1), 1, 1, 1, 1, 1), PriceBar(T0, 1, 1, 1, 1, 1)]
    return InMemoryBarsProvider({"AAA": past + bars(aaa), "BBB": past + bars(bbb), "CCC": past + bars(ccc)})


def test_scoring_never_modifies_decisions_and_uses_only_later_bars(tmp_path):
    db = tmp_path / "s.db"
    journal_with_decisions(db)
    before = decisions_digest(db)
    scorer = ShadowScorer(db, costs=ZERO)
    counts = asyncio.run(scorer.score_pending(provider()))
    assert decisions_digest(db) == before
    assert counts["FINAL"] == 3
    with sqlite3.connect(db) as conn:
        rows = {r[0]: r for r in conn.execute(
            "SELECT decision_id, evaluated, exit_reason, return_pct, fwd_1 FROM shadow_outcomes")}
    trade = rows[1]
    assert trade[1] == "SELECTED" and trade[2] == "target" and trade[3] == pytest.approx(10.0)
    assert trade[4] == pytest.approx(0.0)  # first later bar closes at 100, not the ignored 1.0 bars


def test_no_trade_counterfactual_and_silent_no_trade_are_evaluated(tmp_path):
    db = tmp_path / "s.db"
    journal_with_decisions(db)
    scorer = ShadowScorer(db, costs=ZERO)
    asyncio.run(scorer.score_pending(provider()))
    report = scorer.report()
    assert report["no_trade_counterfactual"]["n"] == 1
    assert report["no_trade_rejected_profitable"] == 1
    with sqlite3.connect(db) as conn:
        silent = conn.execute("SELECT evaluated, fwd_20, status FROM shadow_outcomes WHERE decision_id=3").fetchone()
    assert silent[0] is None and silent[1] == pytest.approx((20 + 19 * 0.1) / 20 * 100 - 100) and silent[2] == "FINAL"


def test_scoring_is_idempotent_and_resumes_pending(tmp_path):
    db = tmp_path / "s.db"
    journal_with_decisions(db)
    scorer = ShadowScorer(db, costs=ZERO)
    short = InMemoryBarsProvider({"AAA": bars([(100, 101, 99, 100)] * 3), "BBB": [], "CCC": bars(FLAT20[:3])})
    first = asyncio.run(scorer.score_pending(short, now=DECIDED + timedelta(days=1)))
    assert first["PENDING_DATA"] == 3 and first["FINAL"] == 0
    second = asyncio.run(scorer.score_pending(provider()))
    assert second["FINAL"] == 3
    third = asyncio.run(scorer.score_pending(provider()))
    assert sum(third.values()) == 0  # nothing left to do
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM shadow_outcomes").fetchone()[0] == 3


def test_mae_mfe_and_timeout():
    series = bars([(100, 102, 97, 101), (101, 104, 99, 103)] + [(103, 103.5, 102.5, 103)] * 3)
    out = score_decision("LONG", 90, 120, series, ZERO, max_bars=5)
    assert out.exit_reason == "timeout" and out.resolved
    assert out.mae_pct == pytest.approx(-3.0) and out.mfe_pct == pytest.approx(4.0)
    open_ended = score_decision("LONG", 90, 120, series[:2], ZERO, max_bars=5)
    assert open_ended.resolved is False


def test_repeated_decisions_on_the_same_bar_are_flagged_and_not_double_counted(tmp_path):
    db = tmp_path / "s.db"
    j, trade, _, _ = journal_with_decisions(db)
    again = j.record_decision(j.new_cycle_id(), symbol="AAA", con_id=1, bar_time=T0, timeframe="1 hour",
                              regime="TRENDING", action="SHADOW_SUBMIT", strategy="breakout_v1", side="LONG",
                              score=70.0, entry=100.0, stop=95.0, target=110.0, reason="repeat",
                              context={"reference_close": 100.0}, created_at=DECIDED)
    scorer = ShadowScorer(db, costs=ZERO)
    counts = asyncio.run(scorer.score_pending(provider()))
    assert counts["DUPLICATE"] == 1 and counts["FINAL"] == 3
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT duplicate_of FROM shadow_decisions WHERE id=?", (again,)).fetchone()[0] == trade
    assert scorer.report()["selected"]["n"] == 1


def test_series_without_the_decision_bar_is_not_scored(tmp_path):
    db = tmp_path / "s.db"
    journal_with_decisions(db)
    scorer = ShadowScorer(db, costs=ZERO)
    # Provider window starts weeks later (e.g. a fixed "last 30 days" request): must not freeze outcomes.
    late = {s: bars(FLAT20, start=500) for s in ("AAA", "BBB", "CCC")}
    counts = asyncio.run(scorer.score_pending(InMemoryBarsProvider(late), now=DECIDED + timedelta(days=1)))
    assert counts["FINAL"] == 0 and counts["PENDING_DATA"] == 3


def test_limit_entry_not_filled_same_day_and_no_target_on_intrabar_fill_bar():
    day = [PriceBar(datetime(2026, 2, 2, 10 + i), 101, 102, 100.5, 101.5, 1) for i in range(3)]
    nxt = [PriceBar(datetime(2026, 2, 3, 10), 90, 91, 89, 90, 1)]
    missed = score_decision("LONG", 95, 110, day + nxt, ZERO, limit=100.0)
    assert (missed.filled, missed.exit_reason, missed.resolved) == (False, "limit_not_filled", True)
    # Intrabar fill at 100 and the same bar also reaches the target: target NOT credited on that bar.
    touch = [PriceBar(datetime(2026, 2, 2, 11), 101, 111, 99.5, 105, 1),
             PriceBar(datetime(2026, 2, 2, 12), 105, 106, 104, 105, 1)] + \
            [PriceBar(datetime(2026, 2, 2, 13) + timedelta(hours=i), 105, 105.5, 104.5, 105, 1) for i in range(60)]
    out = score_decision("LONG", 95, 110, touch, ZERO, limit=100.0)
    assert out.filled and out.entry == 100.0 and out.exit_reason == "timeout"


def test_price_action_before_the_decision_is_not_tradable(tmp_path):
    db = tmp_path / "s.db"
    j = ShadowJournal(db)
    # Decision recorded 2h after its bar: the next bar (which hit the target) started before it.
    j.record_decision(j.new_cycle_id(), symbol="AAA", con_id=1, bar_time=T0, timeframe="1 hour", regime="TRENDING",
                      action="SHADOW_SUBMIT", strategy="s", side="LONG", score=70.0, entry=100.0, stop=95.0,
                      target=110.0, reason="late", context={"reference_close": 100.0},
                      created_at=T0 + timedelta(hours=2))
    series = [PriceBar(T0, 100, 100, 100, 100, 1)] + bars([(100, 111, 99, 110)] + [(100, 101, 99, 100)] * 30)
    scorer = ShadowScorer(db, costs=ZERO)
    asyncio.run(scorer.score_pending(InMemoryBarsProvider({"AAA": series})))
    with sqlite3.connect(db) as conn:
        reason = conn.execute("SELECT exit_reason FROM shadow_outcomes").fetchone()[0]
    assert reason != "target"


def test_mae_is_clipped_at_the_stop_price():
    series = bars([(100, 101, 99, 100), (99, 100, 80, 85)])
    out = score_decision("LONG", 95, 120, series, ZERO)
    assert out.exit_reason == "stop" and out.mae_pct == pytest.approx(-5.0)


def test_report_measures_selected_excess_versus_same_cycle(tmp_path):
    db = tmp_path / "s.db"
    journal_with_decisions(db)
    scorer = ShadowScorer(db, costs=ZERO)
    asyncio.run(scorer.score_pending(provider()))
    report = scorer.report(min_cycle_rows=3)
    # AAA (selected) +10 % over 5 bars; LEAVE-ONE-OUT benchmark = mean of BBB (+10 %) and CCC (+2 %).
    sel = report["selected_vs_cycle_fwd5"]
    assert sel["n"] == 1 and sel["mean_excess_pct"] == pytest.approx(10.0 - (10.0 + 2.0) / 2, abs=0.05)
    # Default protocol minimum (5 rows per cycle): this 3-row cycle is excluded, not averaged.
    default = scorer.report()["selected_vs_cycle_fwd5"]
    assert (default["n"], default["cycles_excluded_small"]) == (0, 1)



def _one(db, action, cycle_open=None):
    j = ShadowJournal(db)
    c = j.new_cycle_id()
    if cycle_open is not None:
        j.record_cycle(c, universe=None, scanners=[], rows_per_scanner=None, quote_budget=None,
                       market_data_type=1, session=None, market_open=cycle_open)
    j.record_decision(c, symbol="AAA", con_id=1, bar_time=T0, timeframe="1 hour", regime="TRENDING",
                      action=action, strategy="breakout_v1", side="LONG", score=70.0, entry=100.0, stop=95.0,
                      target=110.0, reason="x", context={"reference_close": 100.0}, created_at=DECIDED)
    scorer = ShadowScorer(db, costs=ZERO)
    asyncio.run(scorer.score_pending(provider()))
    with sqlite3.connect(db) as conn:
        return conn.execute("SELECT evaluated, filled, exit_reason, executable, fwd_5, provider "
                            "FROM shadow_outcomes").fetchone()


@pytest.mark.parametrize("action,cycle_open,credited", [
    ("SHADOW_SUBMIT", None, True),
    ("PAPER_SUBMITTED", None, True),
    ("SHADOW_BLOCKED", 1, False),          # blocked by a pre-trade check: never a fill
    ("PAPER_BLOCKED", 1, False),
    ("PORTFOLIO_REJECTED", 1, False),
    ("WOULD_LONG", 1, False),              # no portfolio state: never transmitted
    ("APPROVED_LONG", 1, True),            # legacy v1 row, session open
    ("APPROVED_LONG", 0, False),           # legacy v1 row decided after the close: executor refuses
])
def test_only_executable_decisions_are_credited_with_fills(tmp_path, action, cycle_open, credited):
    evaluated, filled, reason, executable, fwd5, provider_name = _one(tmp_path / "s.db", action, cycle_open)
    assert evaluated == "SELECTED" and fwd5 is not None and provider_name == "InMemoryBarsProvider"
    assert bool(executable) is credited
    if not credited:
        assert (filled, reason) == (0, "not_executable")


def test_unalignable_rows_expire_instead_of_staying_pending_forever(tmp_path):
    db = tmp_path / "s.db"
    journal_with_decisions(db)
    scorer = ShadowScorer(db, costs=ZERO)
    late = InMemoryBarsProvider({s: bars(FLAT20, start=500) for s in ("AAA", "BBB", "CCC")})
    counts = asyncio.run(scorer.score_pending(late, now=DECIDED + timedelta(days=15)))
    assert counts["NOT_EVALUABLE"] == 3


def test_provider_error_skips_the_row_without_stopping_the_run(tmp_path):
    db = tmp_path / "s.db"
    journal_with_decisions(db)

    class Flaky(InMemoryBarsProvider):
        def bars_from(self, symbol, con_id, at, timeframe):
            if symbol == "BBB":
                raise ConnectionError("pacing")
            return super().bars_from(symbol, con_id, at, timeframe)
    scorer = ShadowScorer(db, costs=ZERO)
    counts = asyncio.run(scorer.score_pending(Flaky(provider().data)))
    assert counts["PROVIDER_ERROR"] == 1 and counts["FINAL"] == 2


def test_candidate_error_rows_are_not_evaluable(tmp_path):
    db = tmp_path / "s.db"
    j = ShadowJournal(db)
    j.record_decision(j.new_cycle_id(), symbol="AAA", con_id=1, bar_time=None, timeframe="1 hour",
                      regime="UNKNOWN", action="CANDIDATE_ERROR", strategy=None, side=None, score=None,
                      entry=None, stop=None, target=None, reason="TimeoutError")
    counts = asyncio.run(ShadowScorer(db, costs=ZERO).score_pending(provider()))
    assert counts["NOT_EVALUABLE"] == 1
