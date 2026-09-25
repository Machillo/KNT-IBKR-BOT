"""FWD1/FWD2 evaluator on synthetic journals: decision rules, sample minimums, invalid windows."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from random import Random

from research import fwd_protocol
from research.shadow_journal import ShadowJournal
from research.shadow_scoring import SCORER_VERSION, ShadowScorer

START = datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc)


def build(path, *, days=45, cycles_per_day=2, selected_effect=0.0, cf_effect=0.0, noise=0.05, seed=1,
          leak=False):
    rng = Random(seed)
    j = ShadowJournal(path)
    ShadowScorer(path)
    outcomes = []
    sym = 0
    ctx = {"run_mode": "shadow_only", "learning_mode": "frozen", "selector_bonus": 0.0, "session_open": True,
           "sector": "Tech", "bar_count": 140}
    for day in range(days):
        for c in range(cycles_per_day):
            bar = START + timedelta(days=day, hours=c)
            cycle = f"c{day}-{c}"
            j.record_cycle(cycle, universe=None, scanners=[], rows_per_scanner=25, quote_budget=40,
                           market_data_type=1, session="REGULAR", market_open=True)
            base = rng.gauss(0, 0.5)
            for k in range(8):
                sym += 1
                selected = k < 4
                created = bar + timedelta(hours=1, minutes=5)
                if leak and day == 0 and k == 0:
                    created = bar + timedelta(minutes=10)
                did = j.record_decision(
                    cycle, symbol=f"S{sym % 90}", con_id=sym % 90 + 1, bar_time=bar.isoformat(), timeframe="1 hour",
                    regime="TRENDING", action="SHADOW_SUBMIT" if selected else "NO_TRADE",
                    strategy="s" if selected else None, side="LONG" if selected else None, score=70.0,
                    entry=1.0, stop=0.9, target=1.2, reason="x",
                    top={"strategy": "s", "side": "LONG", "score": 40, "entry": 1.0, "stop": 0.9, "target": 1.2},
                    context=ctx, created_at=created)
                fwd5 = base + (selected_effect if selected else cf_effect) + rng.gauss(0, noise)
                outcomes.append((did, SCORER_VERSION, "SELECTED" if selected else "COUNTERFACTUAL", fwd5))
            j.record_cycle_end(cycle, eligible=8, attempted=8, errors=0, max_candidates=12,
                               run_mode="shadow_only", learning_mode="frozen", scanner_rows={}, scanner_errors={})
    with sqlite3.connect(path) as conn:
        conn.executemany(
            "INSERT INTO shadow_outcomes (decision_id, scorer_version, scored_at, status, evaluated, side, "
            "filled, fwd_5, executable, provider) VALUES (?, ?, '', 'FINAL', ?, 'LONG', 0, ?, 1, 'IBKR')", outcomes)
    return path


def test_fwd1_keep_when_selected_clearly_beat_the_cohort_after_costs(tmp_path):
    out = fwd_protocol.evaluate_fwd1(build(tmp_path / "a.db", selected_effect=0.8))
    assert out["decision"] == "KEEP", out


def test_fwd1_reject_when_upper_bound_is_below_cost(tmp_path):
    out = fwd_protocol.evaluate_fwd1(build(tmp_path / "b.db", selected_effect=-0.2))
    assert out["decision"] == "REJECT", out


def test_fwd1_inconclusive_below_minimum_sample(tmp_path):
    out = fwd_protocol.evaluate_fwd1(build(tmp_path / "c.db", days=10, selected_effect=0.8))
    assert out["decision"] == "INCONCLUSIVE" and out["reason"] == "minimum sample not reached"


def test_fwd_evaluation_refuses_an_invalid_evidence_window(tmp_path):
    out = fwd_protocol.evaluate_fwd1(build(tmp_path / "d.db", selected_effect=0.8, leak=True))
    assert out["decision"] == "INCONCLUSIVE" and "invalid" in out["reason"]


def test_fwd2_keep_and_equivalence(tmp_path):
    better = fwd_protocol.evaluate_fwd2(build(tmp_path / "e.db", selected_effect=0.8, cf_effect=0.0))
    assert better["decision"] == "KEEP", better
    same = fwd_protocol.evaluate_fwd2(build(tmp_path / "f.db", selected_effect=0.0, cf_effect=0.0, noise=0.02))
    assert same["decision"] == "REJECT", same
