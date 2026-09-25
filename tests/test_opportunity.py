"""Opportunity records: identical in shadow and replay, recording-only, honest fields, and kept
out of the FWD evidence."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

from backtest.costs import CostModel
from engine.opportunity import Opportunity
from research.pipeline_backtest import PipelineBacktest, PipelineConfig
from test_decision_golden import _walk

ROOT = Path(__file__).resolve().parents[1]


def test_shadow_and_replay_produce_identical_opportunities(tmp_path):
    from engine.shadow import ShadowTradingEngine
    from risk.risk_manager import RiskManager
    from config.config import RiskConfig

    shadow = ShadowTradingEngine(SimpleNamespace(), SimpleNamespace(), risk_manager=RiskManager(RiskConfig()),
                                 state_dir=tmp_path / "s")
    replay = PipelineBacktest({"S1": _walk(1)}, PipelineConfig(cost_model=CostModel.zero()))
    bars = _walk(3)[:140]
    a = shadow.decision_pipeline.decide(bars, symbol="X", asset_class="STK")
    b = replay.pipeline.decide(bars, symbol="X", asset_class="STK")
    assert a.opportunities and a.opportunities == b.opportunities
    assert (a.action, a.reason) == (b.action, b.reason)
    # One record per evaluated strategy; exactly the selected one flagged.
    assert len(a.opportunities) == len(a.selection.evaluations)
    assert sum(o.selected for o in a.opportunities) == (1 if a.selection.selected else 0)


def test_opportunity_fields_are_honest():
    replay = PipelineBacktest({"S1": _walk(1)}, PipelineConfig(cost_model=CostModel.zero()))
    for seed in range(6):
        decision = replay.pipeline.decide(_walk(seed)[:140], symbol="X", asset_class="STK")
        for o in decision.opportunities:
            assert isinstance(o, Opportunity)
            assert o.expected_return_pct is None and o.horizon_bars is None     # unknown is not zero
            assert o.lifecycle == "SHADOW"
            if o.direction == "FLAT":
                assert o.eligibility == "flat" and o.risk_pct is None and o.spread_slip_fee_in_r is None
            elif o.risk_pct:
                assert o.spread_slip_fee_in_r == o.spread_slip_fee_pct / o.risk_pct


def test_shadow_journals_every_opportunity_and_the_fwd_evaluator_never_reads_them(tmp_path):
    from test_shadow_research_path import engine, run

    shadow = engine(tmp_path, n_candidates=2)
    run(shadow)
    conn = sqlite3.connect(shadow.journal.path)
    decisions = conn.execute("SELECT id, strategy FROM shadow_decisions ORDER BY id").fetchall()
    for decision_id, strategy in decisions:
        rows = conn.execute("SELECT strategy, selected, lifecycle FROM shadow_opportunities WHERE decision_id=?",
                            (decision_id,)).fetchall()
        from strategies.lifecycle import status_of
        assert rows and all(r[2] == status_of(r[0]).value for r in rows)    # test strategy -> RESEARCH
        assert [r[0] for r in rows if r[1]] == [strategy]
    for module in ("research/fwd_protocol.py", "research/shadow_scoring.py", "research/shadow_quality.py"):
        assert "shadow_opportunities" not in (ROOT / module).read_text(encoding="utf-8"), module


def test_opportunity_journal_failure_never_breaks_the_decision_journal(tmp_path, monkeypatch):
    from test_shadow_research_path import engine, run

    shadow = engine(tmp_path)
    monkeypatch.setattr(shadow.journal, "record_opportunities",
                        lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("locked")))
    decisions = run(shadow)
    assert decisions[0].action == "SHADOW_SUBMIT"
    assert sqlite3.connect(shadow.journal.path).execute("SELECT COUNT(*) FROM shadow_decisions").fetchone()[0] == 1


def test_stock_type_is_journaled_from_contract_details(tmp_path):
    from test_shadow_research_path import engine, run

    shadow = engine(tmp_path)

    async def details(contract):
        return [SimpleNamespace(industry="Tech", category="x", stockType="ETF")]
    shadow.metadata.ib = SimpleNamespace(reqContractDetailsAsync=details)
    run(shadow)
    row = sqlite3.connect(shadow.journal.path).execute("SELECT stock_type FROM shadow_decisions").fetchone()
    assert row == ("ETF",)



def test_a_recorder_failure_never_changes_the_decision(monkeypatch):
    import engine.decision as decision_module

    replay = PipelineBacktest({"S1": _walk(1)}, PipelineConfig(cost_model=CostModel.zero()))
    bars = _walk(3)[:140]
    expected = replay.pipeline.decide(bars, symbol="X", asset_class="STK")
    monkeypatch.setattr(decision_module, "build_opportunities",
                        lambda *a, **k: (_ for _ in ()).throw(TypeError("recorder bug")))
    broken = replay.pipeline.decide(bars, symbol="X", asset_class="STK")
    assert (broken.action, broken.reason) == (expected.action, expected.reason) and broken.opportunities == ()


def test_short_evaluations_are_not_labelled_eligible_when_shorts_are_disabled():
    from types import SimpleNamespace

    from engine.opportunity import build_opportunities
    from strategies.momentum import SignalSide, StrategySignal

    e = SimpleNamespace(strategy="x", signal=StrategySignal(SignalSide.SHORT, 90, 100.004, 102.0, 96.0, "s"),
                        adjusted_score=90.0, regime_bonus=0.0, evidence_bonus=0.0, learning=None)
    sel = SimpleNamespace(regime=SimpleNamespace(regime=SimpleNamespace(value="TRENDING")), reason="ok",
                          selected=e, evaluations=(e,))
    (o,) = build_opportunities(sel, symbol="X", asset_class="STK", minimum_score=55, context_bars=140)
    assert o.eligibility == "short_disabled" and o.entry == 100.0                # tick-rounded
