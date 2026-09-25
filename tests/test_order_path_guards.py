"""Static guards: order transmission stays in the two guarded places; read-only runners stay read-only."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES = [p for p in ROOT.rglob("*.py") if "tests" not in p.parts and ".git" not in p.parts]

ALLOWED_PLACE_ORDER = {Path("core/order_manager.py"), Path("risk/kill_switch.py")}
ORDER_CAPABLE_NAMES = re.compile(
    r"\b(OrderManager|PaperExecutionEngine|RiskGatedOrderManager|run_broker_smoke_tests|placeOrder|"
    r"bracketOrder|cancelOrder|reqGlobalCancel|whatIfOrder|exerciseOptions)\b"
)
READ_ONLY_RUNNERS = [
    "run_backtest.py", "run_backtest_analysis.py", "run_bulk_research.py", "run_confluence_gen3.py",
    "run_confluence_gen4.py", "run_confluence_lab.py", "run_experiments.py", "run_growth_projection.py",
    "run_import_context_promotions.py", "run_monthly_target_suite.py", "run_pipeline_backtest.py",
    "run_research.py", "run_strategy_suite.py", "run_universe_probe.py", "run_validation_suite.py",
]


def test_place_order_only_in_guarded_modules():
    offenders = []
    for path in SOURCES:
        rel = path.relative_to(ROOT)
        text = path.read_text(encoding="utf-8")
        if re.search(r"\.placeOrder\(", text) and rel not in ALLOWED_PLACE_ORDER:
            offenders.append(str(rel))
    assert offenders == []


def test_guarded_modules_check_the_paper_guard_before_placing():
    om = (ROOT / "core/order_manager.py").read_text(encoding="utf-8")
    assert om.count(".placeOrder(") == 1
    transmit = om[om.index("def _transmit"):om.index("return self.ib.placeOrder")]
    assert "assert_can_transmit" in transmit
    ks = (ROOT / "risk/kill_switch.py").read_text(encoding="utf-8")
    assert ks.count(".placeOrder(") == 1
    before = ks[:ks.index(".placeOrder(")]
    assert before.rfind("assert_can_transmit") > before.rfind("for position in")


def test_read_only_runners_cannot_reach_order_code():
    offenders = {}
    for name in READ_ONLY_RUNNERS:
        text = (ROOT / name).read_text(encoding="utf-8")
        hits = sorted(set(ORDER_CAPABLE_NAMES.findall(text)))
        if hits:
            offenders[name] = hits
    assert offenders == {}
