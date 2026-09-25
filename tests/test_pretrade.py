"""execution/pretrade.py: the pure checks shared by the paper executor, order-free shadow and replay."""
from __future__ import annotations

import pytest

from execution import pretrade
from execution.pretrade import PreTradeContext, PreTradeRequest


def _req(**kw):
    base = dict(side="LONG", quantity=10, entry_price=100.0, stop_price=98.0, target_price=104.0,
                sec_type="STK", reference_price=100.2, market_data_type=1)
    base.update(kw)
    return PreTradeRequest(**base)


def _ctx(**kw):
    base = dict(session_open=True, trading_locked=False, entries_today=0, max_entries_per_day=3)
    base.update(kw)
    return PreTradeContext(**base)


@pytest.mark.parametrize("req,ctx,reason,status", [
    (_req(), _ctx(session_open=False), "market_session_closed", "BLOCKED"),
    (_req(), _ctx(trading_locked=True), "risk_manager_locked", "BLOCKED"),
    (_req(quantity=0), _ctx(), "invalid_execution_request", "REJECTED"),
    (_req(side="FLAT"), _ctx(), "unsupported_side", "REJECTED"),
    (_req(side="SHORT", stop_price=102.0, target_price=96.0), _ctx(), "short_entries_disabled", "REJECTED"),
    (_req(), _ctx(entries_today=3), "daily_entry_limit_reached", "BLOCKED"),
    (_req(sec_type="OPT"), _ctx(), "unsupported_instrument_or_quantity", "REJECTED"),
    (_req(quantity=1.5), _ctx(), "unsupported_instrument_or_quantity", "REJECTED"),
    (_req(stop_price=100.001), _ctx(), "invalid_price_geometry", "REJECTED"),
    (_req(market_data_type=3), _ctx(), "reference_not_live_market_data", "REJECTED"),
    (_req(reference_price=None), _ctx(), "fresh_reference_price_missing", "REJECTED"),
    (_req(reference_price=90.0), _ctx(), "entry_far_from_fresh_reference", "REJECTED"),
    (_req(), _ctx(), None, "PASS"),
])
def test_reason_table(req, ctx, reason, status):
    result = pretrade.evaluate(req, ctx)
    assert (result.reason, result.status) == (reason, status)


def test_order_session_before_lock_before_sanity():
    # Several failures at once: the first in the documented order wins, identically everywhere.
    r = pretrade.evaluate(_req(quantity=0, side="FLAT"), _ctx(session_open=False, trading_locked=True))
    assert r.reason == "market_session_closed"
    r = pretrade.evaluate(_req(quantity=0, side="FLAT"), _ctx(trading_locked=True))
    assert r.reason == "risk_manager_locked"


def test_tick_normalization_happens_before_geometry():
    # 99.996 rounds to 100.00 == entry: geometry must be judged on the transmitted prices.
    r = pretrade.evaluate(_req(stop_price=99.996), _ctx())
    assert r.reason == "invalid_price_geometry"
    assert r.request.stop_price == 100.0


def test_replay_without_reference_passes_but_says_so():
    r = pretrade.evaluate(_req(reference_price=None, market_data_type=None), _ctx(reference_available=False))
    assert r.passed and r.reference_checked is False
    live = pretrade.evaluate(_req(), _ctx())
    assert live.passed and live.reference_checked is True


def test_executor_uses_the_shared_module():
    import inspect

    from execution import paper
    src = inspect.getsource(paper.PaperExecutionEngine.submit)
    assert "pretrade.evaluate(" in src
    assert "reference_available=True" in src  # the executor may never skip the live reference
