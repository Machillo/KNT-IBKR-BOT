import asyncio
from types import SimpleNamespace

import pytest

from config.config import BotConfig, IBKRConfig, RiskConfig, RuntimeConfig
from risk.drawdown_guard import RESET_ACK, DrawdownGuard, DrawdownStateStore

ACCOUNT = "DU0000001"


def guard(tmp_path, pct=0.15):
    return DrawdownGuard(DrawdownStateStore(tmp_path / "dd.json"), ACCOUNT, pct)


def test_high_water_mark_tracks_new_peaks_and_locks_on_drawdown(tmp_path):
    g = guard(tmp_path)
    assert g.evaluate(100_000).record.locked is False
    assert g.evaluate(120_000).record.peak_equity == 120_000
    assert g.evaluate(105_000).record.locked is False          # -12.5 %
    status = g.evaluate(101_000)                               # -15.8 %
    assert status.newly_locked and status.record.locked


def test_lock_is_sticky_across_restarts_and_recovery(tmp_path):
    g = guard(tmp_path)
    g.evaluate(100_000)
    g.evaluate(80_000)
    restarted = guard(tmp_path)
    assert restarted.evaluate(130_000).record.locked is True   # recovery never unlocks by itself
    assert restarted.evaluate(130_000).record.peak_equity == 100_000


def test_reset_needs_literal_ack(tmp_path):
    store = DrawdownStateStore(tmp_path / "dd.json")
    g = DrawdownGuard(store, ACCOUNT, 0.15)
    g.evaluate(100_000)
    g.evaluate(80_000)
    with pytest.raises(PermissionError):
        store.reset(ACCOUNT, 80_000, "yes")
    store.reset(ACCOUNT, 80_000, RESET_ACK)
    assert g.evaluate(80_000).record.locked is False


def test_corrupt_state_fails_closed(tmp_path):
    path = tmp_path / "dd.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(RuntimeError):
        DrawdownGuard(DrawdownStateStore(path), ACCOUNT, 0.15).evaluate(100_000)


def test_invalid_equity_fails_closed(tmp_path):
    with pytest.raises(RuntimeError):
        guard(tmp_path).evaluate(0)


class FakeIB:
    def __init__(self, net_liq):
        self.net_liq = net_liq
        self.client = SimpleNamespace(port=7497)

    def isConnected(self):
        return True

    def managedAccounts(self):
        return [ACCOUNT]

    async def accountSummaryAsync(self, account):
        return [SimpleNamespace(account=ACCOUNT, tag="NetLiquidation", currency="BASE", value=str(self.net_liq))]

    def positions(self):
        return []

    def openTrades(self):
        return []


def test_supervisor_restores_drawdown_lock_on_startup(tmp_path, monkeypatch, isolated_state_dir):
    monkeypatch.chdir(tmp_path)
    from engine.supervisor import PaperSupervisor

    cfg = BotConfig(ibkr=IBKRConfig(port=7497, allow_live_trading=False, account=None),
                    risk=RiskConfig(max_drawdown_pct=0.15), runtime=RuntimeConfig())
    first = PaperSupervisor(FakeIB(100_000), cfg)
    context, _ = asyncio.run(first.initialize())
    assert context.risk.trading_locked is False
    # Later, equity has fallen 20 % over several days (the daily lock alone would not persist).
    second = PaperSupervisor(FakeIB(80_000), cfg)
    monkeypatch.setattr("engine.supervisor.DailyLossGuard.evaluate",
                        lambda self, eq: SimpleNamespace(action_required=False, trading_locked=False,
                                                         state=SimpleNamespace(starting_equity=eq, current_equity=eq,
                                                                               pnl=0.0, loss_pct=0.0)))
    context2, _ = asyncio.run(second.initialize())
    assert context2.risk.trading_locked is True
    assert "drawdown" in context2.risk.lock_reason


def test_missing_state_for_account_with_history_fails_closed(tmp_path):
    g = guard(tmp_path)
    with pytest.raises(RuntimeError):
        g.evaluate(100_000, allow_initialize=False)


def test_readonly_sessions_are_never_verified_as_paper():
    from core.paper_guard import verify_paper_account

    ib = FakeIB(100_000)
    v = verify_paper_account(ib, IBKRConfig(port=7497, allow_live_trading=False, readonly=True))
    assert (v.verified, v.reason) == (False, "readonly_session")


def test_max_drawdown_above_half_is_rejected():
    with pytest.raises(ValueError):
        RiskConfig(max_drawdown_pct=0.9).validate()


def test_drawdown_failure_locks_entries_but_daily_guard_still_runs(isolated_state_dir, monkeypatch):
    from engine.supervisor import PaperSupervisor

    cfg = BotConfig(ibkr=IBKRConfig(port=7497, allow_live_trading=False, account=None),
                    risk=RiskConfig(), runtime=RuntimeConfig())
    sup = PaperSupervisor(FakeIB(100_000), cfg, state_dir=isolated_state_dir)
    context, _ = asyncio.run(sup.initialize())
    calls = []
    original = sup.context.guard.evaluate
    sup.context.guard.evaluate = lambda eq: calls.append(eq) or original(eq)
    monkeypatch.setattr(sup.drawdown, "evaluate", lambda *a, **k: (_ for _ in ()).throw(PermissionError("locked file")))
    asyncio.run(sup.evaluate())
    assert calls, "daily guard / kill switch path must still run"
    assert context.risk.trading_locked and "drawdown guard unavailable" in context.risk.lock_reason


def test_deleted_drawdown_state_with_prior_days_locks_on_startup(isolated_state_dir):
    import json
    from engine.supervisor import PaperSupervisor

    isolated_state_dir.mkdir(parents=True, exist_ok=True)
    (isolated_state_dir / "risk_state.json").write_text(json.dumps({"version": 1, "records": {
        f"{ACCOUNT}:2020-01-02": {"account": ACCOUNT, "trading_date": "2020-01-02", "starting_equity": 100000.0,
                                  "kill_switch_triggered": False, "trigger_reason": "", "updated_at_utc": ""}}}),
        encoding="utf-8")
    cfg = BotConfig(ibkr=IBKRConfig(port=7497, allow_live_trading=False, account=None),
                    risk=RiskConfig(), runtime=RuntimeConfig())
    context, _ = asyncio.run(PaperSupervisor(FakeIB(100_000), cfg, state_dir=isolated_state_dir).initialize())
    assert context.risk.trading_locked and "drawdown guard unavailable" in context.risk.lock_reason
