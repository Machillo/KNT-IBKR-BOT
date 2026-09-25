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
