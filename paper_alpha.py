from __future__ import annotations

import asyncio
import signal

from config.config import config
from core.connection import IBKRConnection
from core.market_data import MarketDataService
from engine.shadow import ShadowTradingEngine
from engine.supervisor import PaperSupervisor
from market.intelligence import MarketIntelligenceService
from utils.logger import logger


async def shadow_loop(engine: ShadowTradingEngine, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await engine.run_once(config.runtime.discovery_rows)
        except Exception as exc:
            logger.exception("SHADOW CYCLE failed | error=%s", exc)
        try:
            await asyncio.wait_for(stop.wait(), timeout=config.runtime.shadow_interval_seconds)
        except asyncio.TimeoutError:
            pass


async def main() -> None:
    config.validate()
    if config.runtime.autonomous_trading_enabled:
        raise RuntimeError("paper_alpha.py is shadow-only; set AUTONOMOUS_TRADING_ENABLED=false")

    connection = IBKRConnection(config.ibkr)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    supervisor_task = None
    shadow_task = None
    try:
        ib = await connection.connect()
        market_data = MarketDataService(ib, config.market_data)
        supervisor = PaperSupervisor(ib, config)
        context, account = await supervisor.initialize()
        if context.risk.trading_locked:
            raise RuntimeError(f"Risk supervisor locked: {context.risk.lock_reason}")

        intelligence = MarketIntelligenceService(ib, market_data)
        shadow = ShadowTradingEngine(ib, intelligence)
        logger.info(
            "PAPER ALPHA SHADOW running | account=%s interval=%ss rows=%s | NO STRATEGY ORDERS",
            account.account, config.runtime.shadow_interval_seconds, config.runtime.discovery_rows,
        )
        supervisor_task = asyncio.create_task(supervisor.run(stop))
        if config.runtime.shadow_trading_enabled:
            shadow_task = asyncio.create_task(shadow_loop(shadow, stop))
        await stop.wait()
    finally:
        stop.set()
        for task in (shadow_task, supervisor_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        await connection.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
