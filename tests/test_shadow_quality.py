"""Data-quality gates and the session report: bad sessions are marked, never deleted."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from research import shadow_quality
from research.shadow_journal import ShadowJournal
from research.shadow_report import render, summarize
from test_shadow_research_path import engine, run

NOW = datetime.now(timezone.utc)


def _clean_session(tmp_path):
    shadow = engine(tmp_path, n_candidates=2)
    run(shadow)
    return shadow.journal.path


def test_clean_shadow_session_is_valid_and_fully_summarized(tmp_path):
    db = _clean_session(tmp_path)
    s = summarize(db)
    assert s["quality"]["verdict"] == "VALID_FOR_RESEARCH", s["quality"]
    assert s["cycles"]["total"] == 1 and s["cycles"]["complete"] == 1
    assert s["cycles"]["learning_modes"] == ["frozen"]
    assert s["decisions"]["canonical"] == 2 and s["decisions"]["trade"] == 2
    assert s["decisions"]["by_action"] == {"SHADOW_SUBMIT": 2}
    assert s["outcomes"]["unscored"] == 2 and s["outcomes"]["by_status"] == {}   # nothing invented
    assert "VALID_FOR_RESEARCH" in render(s)


def _decision(db, **overrides):
    j = ShadowJournal(db)
    cycle = j.new_cycle_id()
    j.record_cycle(cycle, universe=None, scanners=[], rows_per_scanner=25, quote_budget=40,
                   market_data_type=1, session="REGULAR", market_open=True)
    bar = NOW.replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)
    args = dict(symbol="AAA", con_id=1, bar_time=bar.isoformat(), timeframe="1 hour", regime="TRENDING",
                action="SHADOW_SUBMIT", strategy="s", side="LONG", score=70.0, entry=1.0, stop=0.9,
                target=1.2, reason="x", created_at=bar + timedelta(hours=1, minutes=5),
                context={"run_mode": "shadow_only", "learning_mode": "frozen", "selector_bonus": 0.0,
                         "session_open": True, "sector": "Tech", "bar_count": 140, "input_hash": "h"})
    context = {**args.pop("context"), **overrides.pop("context", {})}
    args.update(overrides)
    j.record_decision(cycle, context=context, **args)
    j.record_cycle_end(cycle, eligible=1, attempted=1, errors=0, max_candidates=12,
                       run_mode="shadow_only", learning_mode="frozen", scanner_rows={}, scanner_errors={},
                       config_hash="cfg-test")
    return cycle


def _verdict(db):
    results, session = shadow_quality.evaluate(db)
    return {r.gate for r in results}, session


def test_future_leakage_invalidates_the_session(tmp_path):
    db = tmp_path / "j.db"
    bar = NOW.replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)
    _decision(db, created_at=bar + timedelta(minutes=20))       # before the bar completed
    gates, session = _verdict(db)
    assert "future_leakage" in gates and session["verdict"] == "INVALID_FOR_RESEARCH"


def test_nondeterminism_learning_drift_and_closed_session_transmit_are_zero_tolerance(tmp_path):
    for i, overrides in enumerate([
        {"context": {"learning_mode": "live", "selector_bonus": 4.0}},
        {"context": {"session_open": False}},
    ]):
        db = tmp_path / f"j{i}.db"
        _decision(db, **overrides)
        _, session = _verdict(db)
        assert session["verdict"] == "INVALID_FOR_RESEARCH", overrides
    db = tmp_path / "conflict.db"
    _decision(db)
    j = ShadowJournal(db)
    first = sqlite3.connect(db).execute("SELECT bar_time, cycle_id FROM shadow_decisions").fetchone()
    j.record_decision(first[1], symbol="AAA", con_id=1, bar_time=first[0], timeframe="1 hour", regime="TRENDING",
                      action="NO_TRADE", strategy=None, side=None, score=None, entry=None, stop=None, target=None,
                      reason="flat", context={"run_mode": "shadow_only", "learning_mode": "frozen", "input_hash": "h"})
    gates, session = _verdict(db)
    assert "nondeterministic_decision" in gates and session["verdict"] == "INVALID_FOR_RESEARCH"


def test_incomplete_cycle_and_failed_scanner_are_blocking(tmp_path):
    db = tmp_path / "j.db"
    j = ShadowJournal(db)
    old = j.new_cycle_id()
    j.record_cycle(old, universe=None, scanners=["HOT"], rows_per_scanner=25, quote_budget=40,
                   market_data_type=1, session="REGULAR", market_open=True, run_mode="shadow_only")
    with sqlite3.connect(db) as conn:  # started 3 h ago and never finished
        conn.execute("UPDATE discovery_cycles SET created_at=? WHERE cycle_id=?",
                     ((NOW - timedelta(hours=3)).isoformat(), old))
    c2 = _decision(db)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE discovery_cycles SET scanners='[\"HOT\",\"GAP\"]', scanner_rows='{\"HOT\": 25}', "
                     "scanner_errors='{\"GAP\": \"TimeoutError\"}' WHERE cycle_id=?", (c2,))
    gates, session = _verdict(db)
    assert {"incomplete_cycle", "scanner_failed", "scanner_truncated"} <= gates
    assert session["verdict"] == "INVALID_FOR_RESEARCH" and session["cycles_with_blocking_gate"] == 2


def test_invalid_session_keeps_its_data_and_persists_reasons_idempotently(tmp_path):
    db = tmp_path / "j.db"
    bar = NOW.replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)
    _decision(db, created_at=bar + timedelta(minutes=20))
    with sqlite3.connect(db) as conn:
        before = conn.execute("SELECT * FROM shadow_decisions").fetchall()
    for _ in range(2):
        results, session = shadow_quality.evaluate(db)
        shadow_quality.persist(db, results, session)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT * FROM shadow_decisions").fetchall() == before     # untouched
        rows = conn.execute("SELECT gate, severity FROM research_quality").fetchall()
    assert ("future_leakage", "BLOCKING") in rows
    assert len(rows) == len(set(rows))                                                 # idempotent
    assert ("verdict", "BLOCKING") in rows


def test_legacy_v1_rows_are_excluded_not_mixed(tmp_path):
    db = tmp_path / "j.db"
    c = _decision(db)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE shadow_decisions SET decision_version='selector_v1+decision_pipeline_v1'")
    gates, session = _verdict(db)
    assert "legacy_decision_version" in gates and c in session["excluded_cycles"]


def test_code_or_config_change_inside_the_window_invalidates_it(tmp_path, monkeypatch):
    db = tmp_path / "j.db"
    _decision(db)
    monkeypatch.setattr("research.shadow_journal.code_version", lambda: "docs-only-commit")
    _decision(db)
    gates, session = _verdict(db)
    assert session["verdict"] == "VALID_FOR_RESEARCH", session           # a new HEAD alone is not a change
    monkeypatch.setattr("research.shadow_journal.decision_fingerprint", lambda: "other-decision-code")
    _decision(db)
    gates, session = _verdict(db)
    assert "decision_code_changed" in gates and session["verdict"] == "INVALID_FOR_RESEARCH"
    db2 = tmp_path / "j2.db"
    _decision(db2)
    c = _decision(db2)
    with sqlite3.connect(db2) as conn:
        conn.execute("UPDATE discovery_cycles SET config_hash='changed' WHERE cycle_id=?", (c,))
    gates, session = _verdict(db2)
    assert "config_changed" in gates and session["verdict"] == "INVALID_FOR_RESEARCH"


def test_closed_market_failures_are_recorded_but_do_not_decide_the_verdict(tmp_path):
    db = tmp_path / "j.db"
    _decision(db)
    c = _decision(db)
    with sqlite3.connect(db) as conn:  # an overnight cycle whose scanner failed
        conn.execute("UPDATE discovery_cycles SET market_open=0, scanner_errors='{\"HOT\": \"TimeoutError\"}' "
                     "WHERE cycle_id=?", (c,))
    gates, session = _verdict(db)
    assert "scanner_failed" in gates and session["verdict"] == "VALID_FOR_RESEARCH", session


def test_rows_without_a_run_mode_are_not_part_of_a_modes_window(tmp_path):
    db = tmp_path / "j.db"
    _decision(db)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE discovery_cycles SET run_mode=NULL")
        conn.execute("UPDATE shadow_decisions SET run_mode=NULL")
    _, session = _verdict(db)
    assert session["cycles"] == 0 and session["verdict"] == "INVALID_FOR_RESEARCH"


def test_a_cycle_with_many_failed_candidates_is_blocking(tmp_path):
    db = tmp_path / "j.db"
    c = _decision(db)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE discovery_cycles SET candidates_attempted=10, candidate_errors=3 WHERE cycle_id=?", (c,))
    results, _ = shadow_quality.evaluate(db)
    assert any(r.gate == "candidate_errors" and r.severity == "BLOCKING" for r in results)
