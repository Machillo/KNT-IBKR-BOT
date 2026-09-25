"""Pure pre-transmission checks shared by the paper executor, order-free shadow and replay.

No broker access. The executor still owns everything that needs the broker (paper guard,
connection, broker equity, duplicate/in-flight detection); everything that only depends on
the request and a context lives here, so the three paths refuse the same setups for the same
reasons, in the same order:

    session -> risk lock -> request sanity -> side -> short policy -> daily entry cap
    -> decision-bar freshness -> instrument / whole-share quantity -> tick normalization
    -> price geometry -> fresh live reference

Decision-bar freshness: a signal computed on a bar that completed more than
``max_bar_age_seconds`` ago is refused (``stale_decision_bar``). Without it the first cycles
after the open would transmit on the PREVIOUS session's last bar (~17 h old), which the replay
never does (it refuses decisions available only after the close).

``reference_available=False`` is for replay only (no historical quote tape): the check is then
reported as UNAVAILABLE instead of silently passing.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from core.exceptions import RiskRejectedError

FRESH_MARKET_DATA_TYPES = frozenset({1})
BLOCKED_REASONS = frozenset({"market_session_closed", "risk_manager_locked", "daily_entry_limit_reached"})


@dataclass(frozen=True)
class PreTradeRequest:
    side: str
    quantity: float
    entry_price: float
    stop_price: float
    target_price: float
    sec_type: str = "STK"
    reference_price: float | None = None
    market_data_type: int | None = None
    bar_completed_at: datetime | None = None   # when the decision bar completed (aware)


@dataclass(frozen=True)
class PreTradeContext:
    session_open: bool
    trading_locked: bool
    entries_today: int
    max_entries_per_day: int
    allow_short: bool = False
    max_reference_deviation_pct: float = 0.015
    reference_available: bool = True
    now: datetime | None = None                # the clock; required unless freshness_checked=False
    max_bar_age_seconds: float = 4500.0        # bar size (1 h) + cadence (15 min)
    # Only an explicit False (the intraday replay, where orders are placed at the decision
    # bar's completion) skips the freshness check; a missing clock is otherwise refused.
    freshness_checked: bool = True


@dataclass(frozen=True)
class PreTradeResult:
    reason: str | None           # None = passes every pure check
    status: str                  # PASS | BLOCKED | REJECTED
    request: PreTradeRequest     # normalized when it got that far
    reference_checked: bool

    @property
    def passed(self) -> bool:
        return self.reason is None


def tick(price: float) -> float:
    return float(Decimal(str(price)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def valid_geometry(side: str, entry: float, stop: float, target: float) -> bool:
    side = side.upper()
    if side == "LONG":
        return stop < entry < target
    if side == "SHORT":
        return target < entry < stop
    return False


def _result(reason, request, reference_checked=False) -> PreTradeResult:
    if reason is None:
        return PreTradeResult(None, "PASS", request, reference_checked)
    return PreTradeResult(reason, "BLOCKED" if reason in BLOCKED_REASONS else "REJECTED", request, reference_checked)


def evaluate(request: PreTradeRequest, ctx: PreTradeContext) -> PreTradeResult:
    if not ctx.session_open:
        return _result("market_session_closed", request)
    if ctx.trading_locked:
        return _result("risk_manager_locked", request)
    numbers = (request.quantity, request.entry_price, request.stop_price, request.target_price)
    if not all(isinstance(x, (int, float)) and math.isfinite(x) for x in numbers) or min(numbers) <= 0:
        return _result("invalid_execution_request", request)
    side = request.side.upper()
    if side not in {"LONG", "SHORT"}:
        return _result("unsupported_side", request)
    if side == "SHORT" and not ctx.allow_short:
        return _result("short_entries_disabled", request)
    if ctx.entries_today >= ctx.max_entries_per_day:
        return _result("daily_entry_limit_reached", request)
    if ctx.freshness_checked:
        if ctx.now is None:
            return _result("freshness_clock_missing", request)
        done = request.bar_completed_at
        if done is None or done.tzinfo is None:
            return _result("decision_bar_time_missing", request)
        age = (ctx.now - done).total_seconds()
        if age < 0 or age > ctx.max_bar_age_seconds:
            return _result("stale_decision_bar", request)
    if str(request.sec_type or "").upper() != "STK" or float(request.quantity) != int(request.quantity):
        return _result("unsupported_instrument_or_quantity", request)
    normalized = replace(request, entry_price=tick(request.entry_price), stop_price=tick(request.stop_price),
                         target_price=tick(request.target_price))
    if not valid_geometry(side, normalized.entry_price, normalized.stop_price, normalized.target_price):
        return _result("invalid_price_geometry", normalized)
    if not ctx.reference_available:
        return _result(None, normalized, reference_checked=False)
    if normalized.market_data_type not in FRESH_MARKET_DATA_TYPES:
        return _result("reference_not_live_market_data", normalized)
    ref = normalized.reference_price
    if ref is None or not math.isfinite(ref) or ref <= 0:
        return _result("fresh_reference_price_missing", normalized)
    if abs(normalized.entry_price - ref) / ref > ctx.max_reference_deviation_pct:
        return _result("entry_far_from_fresh_reference", normalized)
    return _result(None, normalized, reference_checked=True)


def hard_risk_refusal(risk_manager, *, equity: float, entry_price: float, stop_price: float,
                      quantity: float) -> str | None:
    """Hard per-trade risk re-check on the NORMALIZED (tick-rounded) prices, or None if approved.

    The caller chooses the equity (the executor uses min(caller, broker NetLiquidation)).
    """
    evaluate = getattr(risk_manager, "evaluate_trade", None)
    if risk_manager is None or evaluate is None:
        return "risk_manager_required"
    try:
        decision = evaluate(equity=float(equity), entry_price=float(entry_price),
                            stop_price=float(stop_price), quantity=float(quantity))
    except RiskRejectedError:
        return "hard_risk:invalid_inputs"
    if not getattr(decision, "approved", False):
        return f"hard_risk:{getattr(decision, 'reason', 'rejected')}"
    return None
