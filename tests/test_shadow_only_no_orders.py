"""run_shadow_only.py can never reach an order/cancel/account-mutation call.

Two independent proofs:
1. static: account-mutating IB calls exist only in the two guarded modules, and the shadow-only
   runner never builds anything that owns one except the (forced dry-run) kill switch;
2. dynamic: the real ``main_async`` runs one full cycle against a recording fake IB in the
   worst case — persisted sticky kill for today, open positions and working orders, ``.env``
   asking for an ARMED kill switch and autonomous trading — and no mutating call is made.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SOURCES = [p for p in ROOT.rglob("*.py") if "tests" not in p.parts and ".git" not in p.parts]
MUTATING = ("placeOrder", "cancelOrder", "reqGlobalCancel", "whatIfOrder", "whatIfOrderAsync",
            "exerciseOptions", "reqAutoOpenOrders", "replaceFA", "reqAccountUpdatesMulti_write")
ALLOWED = {Path("core/order_manager.py"), Path("risk/kill_switch.py")}
ACCOUNT = "DU0000001"


def test_mutating_calls_exist_only_in_the_guarded_modules():
    offenders = []
    for path in SOURCES:
        rel = path.relative_to(ROOT)
        text = path.read_text(encoding="utf-8")
        for name in MUTATING:
            if re.search(rf"\.{name}\(", text) and rel not in ALLOWED:
                offenders.append(f"{rel}:{name}")
    assert offenders == []


class RecordingIB:
    """Answers the read-only calls the shadow-only path makes; records every call; any
    account-mutating method fails the test immediately."""

    def __init__(self):
        self.calls: list[str] = []
        self.client = SimpleNamespace(port=7497)
        contract = SimpleNamespace(symbol="AAA", localSymbol="AAA", conId=1, secType="STK", exchange="SMART",
                                   currency="USD", primaryExchange="NASDAQ")
        self._position = SimpleNamespace(account=ACCOUNT, contract=contract, position=10.0, avgCost=100.0)
        order = SimpleNamespace(account=ACCOUNT, orderId=7, permId=7, action="BUY", totalQuantity=1,
                                orderType="LMT", lmtPrice=99.0, parentId=0, transmit=True)
        self._trade = SimpleNamespace(contract=contract, order=order,
                                      orderStatus=SimpleNamespace(status="Submitted", remaining=1),
                                      isDone=lambda: False)

    def __getattr__(self, name):
        if name in MUTATING:
            raise AssertionError(f"shadow-only reached a mutating call: {name}")
        if name.endswith("Event"):
            return SimpleNamespace(__iadd__=lambda *a: None)

        def unknown(*args, **kwargs):
            self.calls.append(name)
            if name.endswith("Async"):
                async def empty():
                    return []
                return empty()
            return []
        return unknown

    def isConnected(self):
        return True

    def managedAccounts(self):
        return [ACCOUNT]

    async def accountSummaryAsync(self, account=""):
        self.calls.append("accountSummaryAsync")
        return [SimpleNamespace(account=ACCOUNT, tag="NetLiquidation", currency="BASE", value="100000")]

    def positions(self, account=""):
        return [self._position]

    def portfolio(self, account=""):
        return []

    def openTrades(self):
        return [self._trade]


def test_full_shadow_only_cycle_makes_no_mutating_call(isolated_state_dir, monkeypatch):
    import run_shadow_only
    from config.config import BotConfig, IBKRConfig, RiskConfig, RuntimeConfig
    from core.connection import IBKRConnection
    from engine.supervisor import PaperSupervisor

    ib = RecordingIB()
    # Worst-case .env: armed kill switch, autonomous trading on.
    base = BotConfig(ibkr=IBKRConfig(port=7497, allow_live_trading=False, client_id=901),
                     risk=RiskConfig(kill_switch_dry_run=False),
                     runtime=RuntimeConfig(autonomous_trading_enabled=True, require_flat_startup=True))
    monkeypatch.setattr(run_shadow_only, "config", base)
    monkeypatch.setattr(run_shadow_only, "STATE_DIR", isolated_state_dir)

    async def connect(self):
        return ib

    async def disconnect(self):
        return None
    monkeypatch.setattr(IBKRConnection, "connect", connect)
    monkeypatch.setattr(IBKRConnection, "disconnect", disconnect)

    # Persist a sticky kill for today in shadow-only's own state dir (worst case on restart).
    seed_cfg = BotConfig(ibkr=IBKRConfig(port=7497, allow_live_trading=False), risk=RiskConfig(),
                         runtime=RuntimeConfig(require_flat_startup=False))
    seed = PaperSupervisor(ib, seed_cfg, state_dir=isolated_state_dir / "shadow_only")
    asyncio.run(seed.initialize())
    seed.store.mark_triggered(account=ACCOUNT, trading_date=seed.context.trading_date, reason="test sticky kill")

    kill_executions = []
    original = run_shadow_only.shadow_only_config

    def spy_config(cfg):
        out = original(cfg)
        kill_executions.append(out.risk.kill_switch_dry_run)
        return out
    monkeypatch.setattr(run_shadow_only, "shadow_only_config", spy_config)

    asyncio.run(run_shadow_only.main_async(SimpleNamespace(cycles=1)))
    assert kill_executions == [True]
    assert not set(ib.calls) & set(MUTATING)
    assert "accountSummaryAsync" in ib.calls  # the path really ran
