"""Read-only paper preflight: READY only when every required check passes; never mutates."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from config.config import BotConfig, IBKRConfig, MarketDataConfig, RiskConfig, RuntimeConfig
from execution.preflight import broker_checks, config_checks, verdict

ACCOUNT = "DU0000001"


def cfg(**ibkr):
    base = dict(port=7497, allow_live_trading=False, account=None, readonly=True)
    base.update(ibkr)
    return BotConfig(ibkr=IBKRConfig(**base), risk=RiskConfig(kill_switch_enabled=True),
                     runtime=RuntimeConfig(require_flat_startup=True, autonomous_trading_enabled=False),
                     market_data=MarketDataConfig(market_data_type=1))


class FakeIB:
    def __init__(self, positions=(), orders=(), accounts=(ACCOUNT,)):
        self.client = SimpleNamespace(port=7497)
        self._positions, self._orders, self._accounts = list(positions), list(orders), list(accounts)

    def __getattr__(self, name):
        if name in {"placeOrder", "cancelOrder", "reqGlobalCancel", "whatIfOrder"}:
            raise AssertionError(f"preflight reached {name}")
        raise AttributeError(name)

    def isConnected(self):
        return True

    def managedAccounts(self):
        return self._accounts

    async def accountSummaryAsync(self, account=""):
        return [SimpleNamespace(account=ACCOUNT, tag="NetLiquidation", value="100000")]

    def positions(self):
        return self._positions

    async def reqAllOpenOrdersAsync(self):
        return self._orders


class Quotes:
    def __init__(self, data_type=1, bid=100.0, ask=100.02):
        self.snap = SimpleNamespace(market_data_type=data_type, bid=bid, ask=ask)

    async def snapshot_contract(self, contract, symbol=None, timeout=5.0):
        return self.snap


def run(ib, quotes, state_dir, config=None):
    checks, _ = asyncio.run(broker_checks(ib, config or cfg(), state_dir=state_dir, market_data=quotes,
                                          probe_contract=SimpleNamespace(symbol="SPY")))
    return checks


def test_ready_when_everything_checks_out(isolated_state_dir):
    checks = config_checks(cfg(), {}) + run(FakeIB(), Quotes(), isolated_state_dir)
    assert verdict(checks) == ("READY_FOR_SUPERVISED_PLUMBING", [])


def test_each_broker_failure_blocks(isolated_state_dir):
    working = SimpleNamespace(isDone=lambda: False, order=SimpleNamespace(account=ACCOUNT))
    position = SimpleNamespace(account=ACCOUNT, position=1)
    cases = {
        "account_flat": (FakeIB(positions=[position]), Quotes()),
        "no_open_orders_any_client": (FakeIB(orders=[working]), Quotes()),
        "live_two_sided_quote": (FakeIB(), Quotes(data_type=3)),
        "paper_account_verifiable": (FakeIB(accounts=("U0000001",)), Quotes()),
    }
    for name, (ib, quotes) in cases.items():
        _, failed = verdict(run(ib, quotes, isolated_state_dir))
        assert name in failed, (name, failed)


def test_config_failures_and_persisted_acks_block():
    bad = BotConfig(ibkr=IBKRConfig(port=7496, allow_live_trading=True), risk=RiskConfig(kill_switch_enabled=False),
                    runtime=RuntimeConfig(require_flat_startup=False, autonomous_trading_enabled=True),
                    market_data=MarketDataConfig(market_data_type=3))
    _, failed = verdict(config_checks(bad, {"KNT_SIGNAL_PAPER_ACK": "x"}))
    assert set(failed) == {"paper_port", "live_trading_disallowed", "kill_switch_enabled", "market_data_type_live",
                           "require_flat_startup", "autonomous_off", "no_ack_persisted"}


def test_persisted_bot_lock_blocks(isolated_state_dir):
    isolated_state_dir.mkdir(parents=True, exist_ok=True)
    (isolated_state_dir / "drawdown_state.json").write_text("{corrupt", encoding="utf-8")
    _, failed = verdict(run(FakeIB(), Quotes(), isolated_state_dir))
    assert "no_persisted_lock" in failed


def test_plumbing_runner_requires_three_confirmations_and_one_share():
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "run_knt_signal_paper_once.py").read_text(encoding="utf-8")
    assert "--confirm-paper-plumbing" in text and 'dotenv_values(".env").get("KNT_SIGNAL_PAPER_ACK")' in text
    assert "broker_checks(" in text and "MAX_PLUMBING_QTY" in text
    from execution.preflight import MAX_PLUMBING_QTY
    assert MAX_PLUMBING_QTY == 1.0
