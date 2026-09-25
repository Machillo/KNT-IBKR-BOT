"""Opportunity: one (instrument, strategy, direction) evaluation, the unit future selection and
allocation will compare. RECORDING ONLY in this version: decisions are still taken by the
selector (engine/strategy_selector.py) exactly as before; opportunities are journaled so that
strategy x context evidence can be studied later without changing the decision path.

Honest fields only. A value that cannot be computed from what is known at decision time is
None, never a default:

* ``heuristic_score`` is the strategy's own 0-100 confidence formula. Formulas differ per
  strategy (scale and meaning), so scores are NOT comparable across strategies and are NOT
  expected returns.
* ``expected_return_pct`` is always None: no calibrated, forward-validated mapping from a
  signal to an expected return exists (docs/ARCHITECTURE_TARGET.md §Evidence). Unknown != 0.
* ``horizon_bars`` is None: no strategy declares a holding horizon; exits are stop/target only.
* ``cost_pct`` is the round-trip spread + slippage + regulatory-fee estimate of the BASELINE
  cost model, excluding commission (size-dependent: it depends on the allocation).
  ``cost_in_r`` = cost_pct / risk_pct: the share of the stop distance eaten by costs.

Exploratory by construction: these records are NOT FWD evidence (docs/FWD_PROTOCOL.md);
the FWD evaluator never reads them.
"""
from __future__ import annotations

from dataclasses import dataclass

from backtest.costs import BASELINE, CostModel
from strategies.lifecycle import status_of

OPPORTUNITY_VERSION = "opportunity_v1"


@dataclass(frozen=True)
class Opportunity:
    symbol: str
    asset_class: str
    strategy: str
    lifecycle: str
    direction: str                  # LONG | SHORT | FLAT
    regime: str
    heuristic_score: float
    regime_adjustment: float
    evidence_adjustment: float
    adjusted_score: float
    eligibility: str                # eligible | flat | avoided | below_threshold | regime_paused
    selected: bool
    entry: float | None
    stop: float | None
    target: float | None
    risk_pct: float | None          # |entry - stop| / entry, %
    reward_risk: float | None       # |target - entry| / |entry - stop|
    cost_pct: float | None          # round trip, commission excluded, %
    cost_in_r: float | None
    expected_return_pct: None = None
    horizon_bars: int | None = None
    context_bars: int = 0


def _round_trip_cost_pct(costs: CostModel) -> float:
    return (2 * costs.marketable_bps + costs.sell_fee_bps) / 100.0


def build_opportunities(selection, *, symbol: str, asset_class: str, minimum_score: float,
                        context_bars: int, costs: CostModel = BASELINE) -> tuple[Opportunity, ...]:
    regime = selection.regime.regime.value
    paused = selection.reason == "high_volatility_directional_pause"
    chosen = selection.selected
    out = []
    for e in selection.evaluations:
        sig = e.signal
        direction = sig.side.value
        if direction == "FLAT":
            eligibility = "flat"
        elif paused:
            eligibility = "regime_paused"
        elif e.learning is not None and getattr(e.learning.status, "value", "") == "AVOID":
            eligibility = "avoided"
        elif e.adjusted_score < minimum_score:
            eligibility = "below_threshold"
        else:
            eligibility = "eligible"
        entry, stop, target = sig.entry, sig.stop, sig.target
        risk_pct = reward_risk = cost_pct = cost_in_r = None
        if direction != "FLAT" and entry and stop and entry > 0 and entry != stop:
            risk_pct = abs(entry - stop) / entry * 100
            cost_pct = _round_trip_cost_pct(costs)
            cost_in_r = cost_pct / risk_pct
            if target:
                reward_risk = abs(target - entry) / abs(entry - stop)
        out.append(Opportunity(
            symbol=symbol, asset_class=asset_class, strategy=e.strategy, lifecycle=status_of(e.strategy).value,
            direction=direction, regime=regime, heuristic_score=float(sig.score),
            regime_adjustment=float(e.regime_bonus), evidence_adjustment=float(e.evidence_bonus),
            adjusted_score=float(e.adjusted_score), eligibility=eligibility,
            selected=chosen is not None and e.strategy == chosen.strategy,
            entry=entry, stop=stop, target=target, risk_pct=risk_pct, reward_risk=reward_risk,
            cost_pct=cost_pct, cost_in_r=cost_in_r, context_bars=int(context_bars),
        ))
    return tuple(out)
