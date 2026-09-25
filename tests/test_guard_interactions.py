"""A failure in one guard must never stop a more critical one (daily loss -> kill switch)."""
from __future__ import annotations

import asyncio

from config.config import BotConfig, IBKRConfig, RiskConfig, RuntimeConfig
from test_drawdown_guard import FakeIB


def _supervisor(state_dir, net_liq=100_000):
    from engine.supervisor import PaperSupervisor

    cfg = BotConfig(ibkr=IBKRConfig(port=7497, allow_live_trading=False, account=None),
                    risk=RiskConfig(), runtime=RuntimeConfig())
    ib = FakeIB(net_liq)
    sup = PaperSupervisor(ib, cfg, state_dir=state_dir)
    asyncio.run(sup.initialize())
    return sup, ib


def _record_kill(sup):
    calls = []

    async def execute(reason):
        calls.append(reason)
        return None
    sup.kill_switch.execute = execute
    return calls


def test_state_write_failure_does_not_stop_the_kill_switch(isolated_state_dir, monkeypatch):
    sup, ib = _supervisor(isolated_state_dir)
    calls = _record_kill(sup)
    monkeypatch.setattr(sup.store, "mark_triggered",
                        lambda **k: (_ for _ in ()).throw(PermissionError("OneDrive lock")))
    ib.net_liq = 90_000  # -10 %: far beyond the daily cap
    asyncio.run(sup.evaluate())
    assert calls, "kill switch must run even when the sticky state cannot be written"
    assert sup.context.risk.trading_locked
    assert sup._kill_persisted is False  # retried on the next poll


def test_persisted_kill_reason_is_the_daily_breach_not_an_earlier_lock(isolated_state_dir):
    sup, ib = _supervisor(isolated_state_dir)
    calls = _record_kill(sup)
    sup.context.risk.lock_trading("paper_account_unverified: something earlier")
    ib.net_liq = 90_000
    asyncio.run(sup.evaluate())
    assert calls and calls[0].startswith("Daily loss limit reached")
    record, created = sup.store.load_or_create(account=sup.context.account,
                                              trading_date=sup.context.trading_date, starting_equity=1.0)
    assert created is False
    assert record.kill_switch_triggered and record.trigger_reason.startswith("Daily loss limit reached")


def test_drawdown_and_state_failures_together_still_reach_the_kill_switch(isolated_state_dir, monkeypatch):
    sup, ib = _supervisor(isolated_state_dir)
    calls = _record_kill(sup)
    monkeypatch.setattr(sup.drawdown, "evaluate", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    monkeypatch.setattr(sup.store, "mark_triggered", lambda **k: (_ for _ in ()).throw(OSError("disk")))
    ib.net_liq = 90_000
    asyncio.run(sup.evaluate())
    assert calls


def test_long_running_supervisor_measures_daily_loss_from_each_new_day(isolated_state_dir):
    """Day 1 +4 %: day 2 must lock at -10 % from DAY-2 equity, not from the day-1 baseline."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    sup, ib = _supervisor(isolated_state_dir, net_liq=100_000)
    tomorrow = datetime.now(ZoneInfo("America/New_York")) + timedelta(days=1)
    sup._roll_trading_day(104_000, now=tomorrow)
    assert sup.context.trading_date == tomorrow.date().isoformat() and sup.context.starting_equity == 104_000
    result = sup.context.guard.evaluate(93_000)          # -10.6 % from 104k (only -7 % from the old 100k)
    assert result.action_required and sup.context.risk.trading_locked


def test_a_new_day_never_unlocks_an_existing_lock(isolated_state_dir):
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    sup, ib = _supervisor(isolated_state_dir, net_liq=100_000)
    sup.context.risk.lock_trading("multi-day drawdown lock")
    sup._roll_trading_day(100_000, now=datetime.now(ZoneInfo("America/New_York")) + timedelta(days=1))
    assert sup.context.risk.trading_locked and "drawdown" in sup.context.risk.lock_reason


def test_evaluate_itself_rolls_the_baseline_when_the_date_changes(isolated_state_dir):
    import asyncio
    from dataclasses import replace

    sup, ib = _supervisor(isolated_state_dir, net_liq=100_000)
    _record_kill(sup)
    sup.context = replace(sup.context, trading_date="2000-01-03")      # the process started "yesterday"
    ib.net_liq = 104_000
    asyncio.run(sup.evaluate())
    assert sup.context.trading_date != "2000-01-03" and sup.context.starting_equity == 104_000
