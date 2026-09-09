from __future__ import annotations

import asyncio
import signal

from config.config import config
from core.connection import IBKRConnection
from core.market_data import MarketDataService
from engine.shadow import ShadowTradingEngine
from engine.supervisor import PaperSupervisor
from market.intelligence import MarketIntelligenceService
from portfolio.history import PortfolioHistoryStore
from portfolio.state import PortfolioStateService
from utils.logger import logger


async def shadow_loop(
    engine: ShadowTradingEngine,
    supervisor: PaperSupervisor,
    portfolio_state: PortfolioStateService,
    portfolio_history: PortfolioHistoryStore,
    stop: asyncio.Event,
) -> None:
    while not stop.is_set():
        try:
            if supervisor.context is None:
                raise RuntimeError("Supervisor context unavailable")
            account = await supervisor.accounts.snapshot(supervisor.context.account, log=False)
            state = portfolio_state.build(
                account,
                starting_equity=supervisor.context.starting_equity,
                daily_loss_limit_pct=supervisor.context.risk.settings.max_daily_loss_pct,
                trading_locked=supervisor.context.risk.trading_locked,
            )
            portfolio_history.record(account=account.account, state=state)
            logger.info(
                "PORTFOLIO SNAPSHOT | account=%s net_liq=%.2f cash=%.2f committed=%.2f pending=%.2f exposure=%.2f%% daily_loss_used=%.2f/%.2f locked=%s positions=%s orders=%s",
                account.account,
                state.snapshot.net_liquidation,
                state.snapshot.cash,
                state.snapshot.committed_notional,
                state.snapshot.pending_order_notional,
                state.snapshot.gross_exposure_pct * 100,
                state.snapshot.daily_loss_used,
                state.snapshot.daily_loss_limit,
                state.snapshot.trading_locked,
                len(state.positions),
                len(state.pending_orders),
            )
            await engine.run_once(
                config.runtime.discovery_rows,
                portfolio_snapshot=state.snapshot,
            )
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
        shadow = ShadowTradingEngine(
            ib,
            intelligence,
            max_candidates=config.runtime.shadow_max_candidates,
            quote_budget=config.runtime.universe_quote_budget,
        )
        portfolio_state = PortfolioStateService(ib)
        portfolio_history = PortfolioHistoryStore()
        logger.info(
            "PAPER ALPHA SHADOW running | account=%s interval=%ss scanners=4 rows_per_scanner=%s quote_budget=%s deep_candidates=%s | NO STRATEGY ORDERS",
            account.account,
            config.runtime.shadow_interval_seconds,
            config.runtime.discovery_rows,
            config.runtime.universe_quote_budget,
            config.runtime.shadow_max_candidates,
        )
        supervisor_task = asyncio.create_task(supervisor.run(stop))
        if config.runtime.shadow_trading_enabled:
            shadow_task = asyncio.create_task(
                shadow_loop(shadow, supervisor, portfolio_state, portfolio_history, stop)
            )
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
