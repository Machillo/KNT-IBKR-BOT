from __future__ import annotations

import asyncio
import signal

from dotenv import dotenv_values

from config.config import config
from core.connection import IBKRConnection
from core.market_data import MarketDataService
from engine.shadow import ShadowTradingEngine
from engine.supervisor import PaperSupervisor
from core.paper_guard import mask_account
from execution.paper import PaperExecutionEngine, autonomous_paper_armed
from market.intelligence import MarketIntelligenceService
from market.session import BrokerCalendarSessionPolicy
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
                mask_account(account.account),
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
                portfolio_state=state,
            )
        except Exception as exc:
            logger.exception("SHADOW CYCLE failed | error=%s", exc)
        try:
            await asyncio.wait_for(stop.wait(), timeout=config.runtime.shadow_interval_seconds)
        except asyncio.TimeoutError:
            pass


async def main() -> None:
    config.validate()

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
        # Regular-hours clock AND IBKR liquid-hours calendar (holidays/half days); closed until refreshed.
        session_policy = BrokerCalendarSessionPolicy()
        # Orders need BOTH the config flag and the literal per-session ACK, plus a
        # session verified as PAPER by account prefix (not just by port).
        armed = autonomous_paper_armed(
            config.runtime.autonomous_trading_enabled,
            persisted_ack=dotenv_values(".env").get("AUTONOMOUS_PAPER_ACK"),
        )
        if config.runtime.autonomous_trading_enabled and not armed:
            logger.warning("AUTONOMOUS_TRADING_ENABLED=true but AUTONOMOUS_PAPER_ACK missing | executor disabled")
        paper_executor = PaperExecutionEngine(
            ib,
            account=account.account,
            enabled=armed,
            paper_guard=supervisor.paper_guard,
            risk_manager=context.risk,
            session_policy=session_policy,
        )
        shadow = ShadowTradingEngine(
            ib,
            intelligence,
            max_candidates=config.runtime.shadow_max_candidates,
            quote_budget=config.runtime.universe_quote_budget,
            risk_manager=context.risk,
            paper_executor=paper_executor,
            session_policy=session_policy,
        )
        portfolio_state = PortfolioStateService(ib)
        portfolio_history = PortfolioHistoryStore()
        logger.info(
            "PAPER ALPHA running | account=%s interval=%ss scanners=4 rows_per_scanner=%s quote_budget=%s deep_candidates=%s | HARD RISK + PORTFOLIO GATES ACTIVE | autonomous_paper=%s",
            mask_account(account.account),
            config.runtime.shadow_interval_seconds,
            config.runtime.discovery_rows,
            config.runtime.universe_quote_budget,
            config.runtime.shadow_max_candidates,
            armed,
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
