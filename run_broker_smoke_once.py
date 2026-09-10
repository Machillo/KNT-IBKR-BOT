from __future__ import annotations

import asyncio
from os import getenv

from config.config import config
from core.connection import IBKRConnection
from core.market_data import MarketDataService
from engine.supervisor import PaperSupervisor
from main import run_broker_smoke_tests

ACK = "I_UNDERSTAND_THIS_SUBMITS_PAPER_MARKET_ORDERS"


async def main() -> None:
    config.validate()
    if config.ibkr.port not in config.ibkr.paper_ports:
        raise RuntimeError(f"One-shot broker smoke requires Paper port; got {config.ibkr.port}")
    if config.ibkr.allow_live_trading:
        raise RuntimeError("Set ALLOW_LIVE_TRADING=false")
    if config.ibkr.readonly:
        raise RuntimeError("Set IBKR_READONLY=false for this Paper execution test")
    if getenv("BROKER_SMOKE_ACK", "") != ACK:
        raise RuntimeError(f"Set BROKER_SMOKE_ACK={ACK} for this one run")

    connection = IBKRConnection(config.ibkr)
    try:
        ib = await connection.connect()
        market_data = MarketDataService(ib, config.market_data)
        supervisor = PaperSupervisor(ib, config)
        context, account = await supervisor.initialize()
        if context.risk.trading_locked:
            raise RuntimeError(f"Broker smoke blocked by risk lock: {context.risk.lock_reason}")
        await run_broker_smoke_tests(
            ib,
            market_data,
            account.net_liquidation,
            context.risk,
            account.account,
        )
    finally:
        await connection.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
