"""Explicit strategy lifecycle: which strategies may be evaluated, and which may trade on paper.

A strategy's status is part of the SOURCE CODE and changes only through a reviewed commit
(docs/ARCHITECTURE_TARGET.md §Strategy lifecycle). Nothing at runtime — learning, scoring,
research schedulers — can promote a strategy: there is deliberately no setter.

    RESEARCH           hypothesis only: offline research; never evaluated by the selector
    SHADOW             evaluated order-free (shadow-only journal); no capital, not paper-tradable
    FORWARD_VALIDATED  passed a pre-registered forward test (e.g. FWD-vN KEEP); still no capital
    PAPER              eligible for autonomous PAPER trading, after a human review of the evidence
    LIVE_ELIGIBLE      eligible for a LIVE gate review (docs/paper-to-live gate); never automatic
    RETIRED            removed from selection; kept for the record

Unknown strategy names are RESEARCH (fail closed). Today every strategy is SHADOW: none has
forward evidence (docs/experiments/LOG.md: 15/15 validation tests REJECT; FWD1-3 pending).
"""
from __future__ import annotations

from enum import Enum
from types import MappingProxyType


class StrategyStatus(str, Enum):
    RESEARCH = "RESEARCH"
    SHADOW = "SHADOW"
    FORWARD_VALIDATED = "FORWARD_VALIDATED"
    PAPER = "PAPER"
    LIVE_ELIGIBLE = "LIVE_ELIGIBLE"
    RETIRED = "RETIRED"


_REGISTRY = {
    "breakout_v1": StrategyStatus.SHADOW,
    "momentum_gap_v1": StrategyStatus.SHADOW,
    "swing_structure_v1": StrategyStatus.SHADOW,
    "trend_following_v1": StrategyStatus.SHADOW,
    "mean_reversion_v1": StrategyStatus.SHADOW,
    "range_v1": StrategyStatus.SHADOW,
    "smc_liquidity_v1": StrategyStatus.SHADOW,
    "fib_trend_pullback_v1": StrategyStatus.SHADOW,
    "liquidity_fib_reversal_v1": StrategyStatus.SHADOW,
    "structure_sr_confluence_v1": StrategyStatus.SHADOW,
}
REGISTRY = MappingProxyType(_REGISTRY)  # read-only view

# Evaluated by the selector (shadow, replay, runtime).
SELECTABLE = frozenset({StrategyStatus.SHADOW, StrategyStatus.FORWARD_VALIDATED, StrategyStatus.PAPER,
                        StrategyStatus.LIVE_ELIGIBLE})
# May be transmitted by the autonomous paper executor.
PAPER_TRADABLE = frozenset({StrategyStatus.PAPER, StrategyStatus.LIVE_ELIGIBLE})
# The supervised 1-share plumbing test validates the ORDER PATH, not a strategy: any selectable
# strategy may be used there, and only there (run_knt_signal_paper_once.py).
PLUMBING_ONLY = SELECTABLE


def status_of(strategy: str | None) -> StrategyStatus:
    return REGISTRY.get(str(strategy or ""), StrategyStatus.RESEARCH)
