import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from backtest.costs import CostModel
from market.history import PriceBar
from research.shadow_journal import ShadowJournal, score_decision

T0 = datetime(2026, 1, 5, 10)
ZERO = CostModel.zero()


def bar(i, o, h, l, c):
    return PriceBar(T0 + timedelta(hours=i), o, h, l, c, 1000)


def test_journal_records_point_in_time_discovery_and_every_decision(tmp_path):
    j = ShadowJournal(tmp_path / "s.db")
    cycle = j.new_cycle_id()
    ranked = [SimpleNamespace(symbol="AAA", contract=SimpleNamespace(conId=1), scanner_rank=0, score=90.0,
                              eligible=True, reference_price=10.0, spread_bps=5.0, reason="ok"),
              SimpleNamespace(symbol="BBB", contract=SimpleNamespace(conId=2), scanner_rank=3, score=0.0,
                              eligible=False, reference_price=None, spread_bps=None, reason="spread")]
    assert j.record_discovery(cycle, ranked) == 2
    j.record_decision(cycle, symbol="AAA", con_id=1, bar_time=T0, timeframe="1 hour", regime="MIXED",
                      action="NO_TRADE", strategy=None, side=None, score=None, entry=None, stop=None,
                      target=None, reason="best_signal_below_threshold")
    with sqlite3.connect(j.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM discovery_snapshots WHERE cycle_id=?", (cycle,)).fetchone()[0] == 2
        assert conn.execute("SELECT action FROM shadow_decisions").fetchone()[0] == "NO_TRADE"


def test_score_long_target_and_stop_gap():
    win = score_decision("LONG", 95, 110, [bar(1, 100, 101, 99, 100), bar(2, 100, 111, 99, 110)], ZERO)
    assert (win.exit_reason, win.return_pct) == ("target", pytest.approx(10.0))
    gap = score_decision("LONG", 95, 110, [bar(1, 100, 101, 99, 100), bar(2, 90, 91, 89, 90)], ZERO)
    assert (gap.exit_reason, gap.exit) == ("stop_gap", 90.0)


def test_score_stop_wins_ties_and_short_side():
    tie = score_decision("LONG", 95, 110, [bar(1, 100, 112, 94, 100)], ZERO)
    assert tie.exit_reason == "stop"
    short = score_decision("SHORT", 105, 90, [bar(1, 100, 101, 99, 100), bar(2, 95, 96, 89, 90)], ZERO)
    assert short.exit_reason == "target" and short.return_pct == pytest.approx(10.0)


def test_score_refuses_gapped_bracket_and_times_out():
    assert score_decision("LONG", 95, 110, [bar(1, 120, 121, 119, 120)], ZERO).filled is False
    flat = [bar(i, 100, 101, 99, 100) for i in range(1, 6)]
    out = score_decision("LONG", 95, 110, flat, ZERO, max_bars=5)
    assert out.exit_reason == "timeout" and out.bars_held == 5


def test_score_costs_reduce_return():
    bars = [bar(1, 100, 101, 99, 100), bar(2, 100, 111, 99, 110)]
    assert score_decision("LONG", 95, 110, bars).return_pct < score_decision("LONG", 95, 110, bars, ZERO).return_pct
