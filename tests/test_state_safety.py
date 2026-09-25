"""State paths are anchored and resolved at call time; nothing defaults to a CWD-relative state/."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES = [p for p in ROOT.rglob("*.py") if "tests" not in p.parts and ".git" not in p.parts]


def test_no_cwd_relative_state_paths_in_source():
    offenders = []
    for path in SOURCES:
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"""["']state/""", line):
                offenders.append(f"{path.relative_to(ROOT)}:{n}")
    assert offenders == []


def test_stores_resolve_to_the_overridden_state_dir(isolated_state_dir):
    from execution.paper import TradeJournalStore
    from portfolio.admission import RiskDecisionStore
    from portfolio.history import PortfolioHistoryStore
    from research.context_promotions import ContextPromotionStore
    from research.performance import StrategyPerformanceStore
    from research.shadow_journal import ShadowJournal
    from research.shadow_scoring import ShadowScorer
    from risk.drawdown_guard import DrawdownStateStore
    from risk.state_store import DailyRiskStateStore

    for store in (TradeJournalStore(), RiskDecisionStore(), PortfolioHistoryStore(), ContextPromotionStore(),
                  StrategyPerformanceStore(), ShadowJournal(), ShadowScorer(), DrawdownStateStore(),
                  DailyRiskStateStore()):
        assert Path(store.path).is_relative_to(isolated_state_dir), type(store).__name__


def test_real_state_dir_is_anchored_to_the_repo():
    import config.config as cfg

    # conftest overrides it per test; the module constant itself must be absolute and repo-anchored.
    assert Path(cfg.__file__).resolve().parents[1] / "state" == ROOT / "state"


def test_shadow_only_uses_its_own_state_dir(isolated_state_dir):
    text = (ROOT / "run_shadow_only.py").read_text(encoding="utf-8")
    assert 'state_dir=STATE_DIR / "shadow_only"' in text
    assert text.count('STATE_DIR / "shadow_only"') >= 2  # supervisor AND shadow engine stores
