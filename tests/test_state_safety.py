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


def test_research_defaults_point_at_the_shadow_only_journal_and_anchored_reports(isolated_state_dir):
    from config.config import REPORTS_DIR, shadow_journal_path
    from research.shadow_scoring import CacheBarsProvider

    assert shadow_journal_path() == isolated_state_dir / "shadow_only" / "strategy_performance.db"
    assert CacheBarsProvider().cache_dir == ROOT / "reports" / "history_cache" == REPORTS_DIR / "history_cache"


def test_journal_universe_can_be_restricted_to_one_run_mode(tmp_path):
    import sqlite3
    from datetime import datetime

    from research.shadow_journal import ShadowJournal
    from research.universe_provider import JournalUniverse

    db = tmp_path / "j.db"
    ShadowJournal(db)
    with sqlite3.connect(db) as conn:
        for cid, mode, sym in (("a", "shadow_only", "SHADOW"), ("b", "runtime", "RUNTIME")):
            conn.execute("INSERT INTO discovery_cycles (cycle_id, created_at, run_mode) VALUES (?, ?, ?)",
                         (cid, "2026-03-02T15:00:00+00:00" if cid == "a" else "2026-03-02T15:30:00+00:00", mode))
            conn.execute("INSERT INTO discovery_funnel (cycle_id, created_at, symbol, status) VALUES (?, '', ?, 'ranked_eligible')",
                         (cid, sym))
    at = datetime(2026, 3, 2, 11, 0)
    assert JournalUniverse(db).members_at(at) == {"RUNTIME"}
    assert JournalUniverse(db, run_mode="shadow_only").members_at(at) == {"SHADOW"}
