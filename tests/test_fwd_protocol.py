"""FWD1/FWD2 evaluator on synthetic journals: decision rules, sample minimums, invalid windows."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from random import Random

from research import fwd_protocol
from research.shadow_journal import ShadowJournal
from research.shadow_scoring import SCORER_VERSION, ShadowScorer

START = datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc)


import pytest


COMMITTED: dict = {}
REAL_COMMITTED_LINES = fwd_protocol.committed_registration_lines   # captured before any patching


@pytest.fixture(autouse=True)
def committed_log(monkeypatch):
    """Stand-in for 'git log -S' over docs/experiments/LOG.md: lines 'committed' by the test."""
    COMMITTED.clear()
    monkeypatch.setattr(fwd_protocol, "committed_registration_lines", lambda: dict(COMMITTED))
    return COMMITTED


def _register(path, start, record_in_log=True, config_hash="cfg-test"):
    record = fwd_protocol.register_window(path, config_hash=config_hash, start=start)
    if record_in_log:
        COMMITTED[fwd_protocol.registration_line(record)] = datetime.now(timezone.utc)
    return record


def build(path, *, days=45, cycles_per_day=2, selected_effect=0.0, cf_effect=0.0, noise=0.05, seed=1,
          leak=False, late_effect=None, register=True, unscored_day=None, start=START, record_in_log=True,
          registered_config="cfg-test"):
    rng = Random(seed)
    if register:
        COMMITTED.clear()          # each synthetic journal stands for its own repository history
        _register(path, datetime.now(timezone.utc) - timedelta(minutes=1), record_in_log, registered_config)
    j = ShadowJournal(path)
    ShadowScorer(path)
    outcomes = []
    sym = 0
    ctx = {"run_mode": "shadow_only", "learning_mode": "frozen", "selector_bonus": 0.0, "session_open": True,
           "sector": "Tech", "bar_count": 140}
    for day in range(days):
        for c in range(cycles_per_day):
            bar = start + timedelta(days=day, hours=c)
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
                effect = selected_effect if late_effect is None or day < 40 else late_effect
                fwd5 = base + (effect if selected else cf_effect) + rng.gauss(0, noise)
                if unscored_day is None or day != unscored_day:
                    outcomes.append((did, SCORER_VERSION, "SELECTED" if selected else "COUNTERFACTUAL", fwd5))
            j.record_cycle_end(cycle, eligible=8, attempted=8, errors=0, max_candidates=12,
                               run_mode="shadow_only", learning_mode="frozen", scanner_rows={}, scanner_errors={},
                               config_hash="cfg-test")
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
    assert out["decision"] == "INCONCLUSIVE" and out["reason"].startswith("monitoring only")
    assert out["window"]["binding"] is False


def test_fwd_evaluation_refuses_an_invalid_evidence_window(tmp_path):
    out = fwd_protocol.evaluate_fwd1(build(tmp_path / "d.db", selected_effect=0.8, leak=True))
    assert out["decision"] == "INCONCLUSIVE" and "invalid" in out["reason"]


def test_fwd2_keep_and_equivalence(tmp_path):
    better = fwd_protocol.evaluate_fwd2(build(tmp_path / "e.db", selected_effect=0.8, cf_effect=0.0))
    assert better["decision"] == "KEEP", better
    same = fwd_protocol.evaluate_fwd2(build(tmp_path / "f.db", selected_effect=0.0, cf_effect=0.0, noise=0.02))
    assert same["decision"] == "REJECT", same



def test_data_after_the_binding_cutoff_is_never_used(tmp_path):
    # Minimums are met on day 40 (-0.2 % effect -> REJECT); a strong later effect must not flip it.
    out = fwd_protocol.evaluate_fwd1(build(tmp_path / "g.db", days=60, selected_effect=-0.2, late_effect=2.0))
    assert out["window"]["binding"] is True and out["sample"]["days"] == 40
    assert out["decision"] == "REJECT", out


def test_student_t_critical_values():
    assert abs(fwd_protocol.t_quantile(0.975, 10) - 2.228) < 1e-3
    assert abs(fwd_protocol.critical_t(10_000) - 2.394) < 1e-2        # Bonferroni K=3 -> normal limit
    assert fwd_protocol.critical_t(40) > 2.45                          # small samples need more



def test_unregistered_window_is_never_evaluated(tmp_path):
    out = fwd_protocol.evaluate_fwd1(build(tmp_path / "h.db", selected_effect=0.8, register=False))
    assert out["decision"] == "INCONCLUSIVE" and "not registered" in out["reason"]
    fwd_protocol.register_window(tmp_path / "x.db", config_hash="c")
    with pytest.raises(FileExistsError):
        fwd_protocol.register_window(tmp_path / "x.db", config_hash="c")   # registered once, never moved


def test_registration_must_be_committed_to_the_log(tmp_path):
    out = fwd_protocol.evaluate_fwd1(build(tmp_path / "l.db", selected_effect=0.8, record_in_log=False))
    assert out["decision"] == "INCONCLUSIVE" and "not committed" in out["reason"]


def test_config_differing_from_the_registration_invalidates_the_window(tmp_path):
    out = fwd_protocol.evaluate_fwd1(build(tmp_path / "m.db", selected_effect=0.8, registered_config="intended"))
    assert out["decision"] == "INCONCLUSIVE" and "invalid" in out["reason"]


def test_cutoff_is_counted_from_decisions_and_waits_for_scoring(tmp_path):
    # Day 10 rows are not scored yet: the cutoff (day 40) is the same as with full scoring,
    # but the result is monitoring-only until those rows are scored.
    out = fwd_protocol.evaluate_fwd1(build(tmp_path / "i.db", selected_effect=0.8, unscored_day=10))
    assert out["window"]["minimums_reached"] is True and out["window"]["binding"] is False
    assert out["reason"] == "monitoring only: scoring incomplete up to the cutoff"
    full = fwd_protocol.evaluate_fwd1(build(tmp_path / "j.db", selected_effect=0.8))
    assert out["window"]["cutoff_day"] == full["window"]["cutoff_day"]



def test_cutoff_never_lands_after_the_deadline(tmp_path):
    # Minimums would only be reached around 2027-04-18, after the 2027-03-31 deadline.
    late_start = datetime(2027, 3, 10, 14, 0, tzinfo=timezone.utc)
    out = fwd_protocol.evaluate_fwd1(build(tmp_path / "k.db", selected_effect=0.8, start=late_start))
    assert out["window"]["minimums_reached"] is False
    assert out["window"]["cutoff_day"] <= fwd_protocol.EVIDENCE_DEADLINE.isoformat()
    assert out["decision"] == "INCONCLUSIVE"



def test_interim_statistics_are_blinded_until_binding(tmp_path):
    out = fwd_protocol.evaluate_fwd1(build(tmp_path / "n.db", days=10, selected_effect=0.8))
    assert out["window"]["binding"] is False
    assert out["gross_excess"] == "BLINDED" and out["net_excess"] == "BLINDED" and out["critical_t"] == "BLINDED"
    binding = fwd_protocol.evaluate_fwd1(build(tmp_path / "o.db", selected_effect=0.8))
    assert binding["window"]["binding"] is True and isinstance(binding["gross_excess"], dict)


def test_second_registration_or_protocol_change_or_ending_never_binds(tmp_path, monkeypatch):
    db = build(tmp_path / "p.db", selected_effect=0.8)
    assert fwd_protocol.evaluate_fwd1(db)["decision"] == "KEEP"
    # A second registration line ever committed (e.g. delete + re-register) -> never binds.
    COMMITTED["FWD-v1 WINDOW REGISTERED: start_utc=later ..."] = datetime.now(timezone.utc)
    assert "registrations were committed" in fwd_protocol.evaluate_fwd1(db)["reason"]
    COMMITTED.pop("FWD-v1 WINDOW REGISTERED: start_utc=later ...")
    # The evaluator/scorer/gates changed since registration -> refused.
    monkeypatch.setattr(fwd_protocol, "protocol_fingerprint", lambda: "edited")
    assert "changed since registration" in fwd_protocol.evaluate_fwd1(db)["reason"]
    monkeypatch.undo()
    monkeypatch.setattr(fwd_protocol, "committed_registration_lines", lambda: dict(COMMITTED))
    # Ending the window is recorded, never silent, and never binding.
    fwd_protocol.end_window(db, "config changed")
    out = fwd_protocol.evaluate_fwd1(db)
    assert out["decision"] == "INCONCLUSIVE" and "window ended" in out["reason"]


def test_registration_cannot_be_backdated_and_commit_must_be_prompt(tmp_path):
    with pytest.raises(ValueError):
        fwd_protocol.register_window(tmp_path / "q.db", config_hash="c",
                                     start=datetime.now(timezone.utc) - timedelta(days=2))
    db = tmp_path / "r.db"
    record = fwd_protocol.register_window(db, config_hash="cfg-test")
    COMMITTED[fwd_protocol.registration_line(record)] = datetime.now(timezone.utc) + timedelta(days=10)
    ShadowJournal(db)
    assert "outside the allowed delay" in fwd_protocol.evaluate_fwd1(db)["reason"]


def test_cycles_of_the_registered_code_before_the_start_are_a_pre_registration_look(tmp_path):
    db = tmp_path / "s.db"
    j = ShadowJournal(db)
    j.record_cycle("early", universe=None, scanners=[], rows_per_scanner=25, quote_budget=40,
                   market_data_type=1, session="REGULAR", market_open=True, run_mode="shadow_only")
    j.record_cycle_end("early", eligible=0, attempted=0, errors=0, max_candidates=12, run_mode="shadow_only",
                       learning_mode="frozen", scanner_rows={}, scanner_errors={}, config_hash="cfg-test")
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE discovery_cycles SET created_at=?",
                     ((datetime.now(timezone.utc) - timedelta(days=3)).isoformat(),))
    _register(db, datetime.now(timezone.utc))
    assert "pre-registration look" in fwd_protocol.evaluate_fwd1(db)["reason"]


def test_committed_registration_lines_reads_git_history_including_deleted_lines(tmp_path):
    import subprocess

    from research.fwd_protocol import REGISTRATION_PREFIX

    repo = tmp_path / "repo"
    (repo / "docs" / "experiments").mkdir(parents=True)
    log = repo / "docs" / "experiments" / "LOG.md"

    def git(*args):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True,
                       env={**__import__("os").environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
    git("init", "-q")
    first = f"{REGISTRATION_PREFIX} start_utc=A"
    log.write_text("# log\n" + first + "\n", encoding="utf-8")
    git("add", "."); git("commit", "-qm", "register A")
    log.write_text("# log\n", encoding="utf-8")                     # someone deletes it...
    git("commit", "-qam", "remove A")
    second = f"{REGISTRATION_PREFIX} start_utc=B"
    log.write_text("# log\n" + second + "\n", encoding="utf-8")
    git("commit", "-qam", "register B")
    log.write_text("# log\n" + second + "\n" + f"{REGISTRATION_PREFIX} start_utc=UNCOMMITTED\n", encoding="utf-8")
    lines = REAL_COMMITTED_LINES(repo)
    assert set(lines) == {first, second}                            # deleted one still counts; uncommitted not
