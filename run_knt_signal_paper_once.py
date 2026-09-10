from __future__ import annotations

import asyncio
from dataclasses import replace
from os import getenv

from config.config import config
from core.broker_state import BrokerStateService
from core.connection import IBKRConnection
from core.market_data import MarketDataService
from engine.shadow import ShadowTradingEngine
from engine.supervisor import PaperSupervisor
from execution.paper import PaperExecutionEngine, PaperExecutionRequest, PaperExecutionResult
from market.intelligence import MarketIntelligenceService
from market.session import USStockSessionPolicy
from portfolio.state import PortfolioStateService
from utils.logger import logger

ACK = "I_UNDERSTAND_KNT_WILL_SUBMIT_ONE_PAPER_SIGNAL"


class OneShotCappedPaperExecutor:
    """Allow at most one accepted Paper bracket and cap validation quantity.

    The cap is intentionally stricter than portfolio sizing. This runner validates the
    strategy -> admission -> broker path; it is not a sizing validation run.
    """

    def __init__(self, executor: PaperExecutionEngine, *, max_quantity: float = 1.0) -> None:
        if max_quantity <= 0:
            raise ValueError("max_quantity must be > 0")
        self.executor = executor
        self.max_quantity = float(max_quantity)
        self.submitted = 0
        self.last_request: PaperExecutionRequest | None = None
        self.last_result: PaperExecutionResult | None = None

    async def submit(self, contract, request: PaperExecutionRequest) -> PaperExecutionResult:
        if self.submitted >= 1:
            result = PaperExecutionResult(False, "one_shot_submission_limit_reached")
            self.last_result = result
            return result

        capped = replace(request, quantity=min(float(request.quantity), self.max_quantity))
        self.last_request = capped
        result = await self.executor.submit(contract, capped)
        self.last_result = result
        if result.submitted:
            self.submitted += 1
        return result


async def main() -> None:
    config.validate()
    if config.ibkr.port not in config.ibkr.paper_ports:
        raise RuntimeError(f"KNT signal probe requires an IBKR Paper port; got {config.ibkr.port}")
    if config.ibkr.allow_live_trading:
        raise RuntimeError("Set ALLOW_LIVE_TRADING=false")
    if config.ibkr.readonly:
        raise RuntimeError("Set IBKR_READONLY=false for this Paper signal probe")
    if not config.runtime.require_flat_startup:
        raise RuntimeError("Set REQUIRE_FLAT_STARTUP=true for this Paper signal probe")
    if config.runtime.autonomous_trading_enabled:
        raise RuntimeError("Set AUTONOMOUS_TRADING_ENABLED=false; this runner authorizes one probe only")
    if getenv("KNT_SIGNAL_PAPER_ACK", "") != ACK:
        raise RuntimeError(f"Set KNT_SIGNAL_PAPER_ACK={ACK} for this one run")

    max_qty = float(getenv("KNT_SIGNAL_PAPER_MAX_QTY", "1"))
    if max_qty <= 0 or max_qty > 10:
        raise RuntimeError("KNT_SIGNAL_PAPER_MAX_QTY must be > 0 and <= 10")

    connection = IBKRConnection(config.ibkr)
    try:
        ib = await connection.connect()
        supervisor = PaperSupervisor(ib, config)
        context, account = await supervisor.initialize()
        if context.risk.trading_locked:
            raise RuntimeError(f"KNT signal probe blocked by risk lock: {context.risk.lock_reason}")

        broker = BrokerStateService(ib)
        before = broker.snapshot(account.account)
        if not before.is_flat:
            raise RuntimeError(
                f"KNT signal probe requires flat broker state; positions={before.position_count} "
                f"open_orders={before.open_order_count}"
            )

        session_policy = USStockSessionPolicy()
        session = session_policy.state()
        if not session.market_open:
            raise RuntimeError(
                f"KNT signal probe requires regular US stock session; state={session.session} "
                f"local={session.local_time.isoformat()}"
            )

        market_data = MarketDataService(ib, config.market_data)
        intelligence = MarketIntelligenceService(ib, market_data)
        portfolio_state = PortfolioStateService(ib).build(
            account,
            starting_equity=context.starting_equity,
            daily_loss_limit_pct=config.risk.max_daily_loss_pct,
            trading_locked=context.risk.trading_locked,
        )

        raw_executor = PaperExecutionEngine(
            ib,
            account=account.account,
            enabled=True,
            paper_authorized=True,
            risk_manager=context.risk,
            session_policy=session_policy,
        )
        executor = OneShotCappedPaperExecutor(raw_executor, max_quantity=max_qty)
        shadow = ShadowTradingEngine(
            ib,
            intelligence,
            minimum_signal_score=55.0,
            max_candidates=config.runtime.shadow_max_candidates,
            quote_budget=config.runtime.universe_quote_budget,
            research_budget=0,
            risk_manager=context.risk,
            paper_executor=executor,
        )

        logger.warning(
            "KNT SIGNAL PAPER PROBE | dynamic_discovery=True max_paper_submissions=1 max_qty=%s",
            max_qty,
        )
        decisions = await shadow.run_once(
            rows_per_scanner=config.runtime.discovery_rows,
            portfolio_state=portfolio_state,
        )

        submitted = [d for d in decisions if d.action == "PAPER_SUBMITTED"]
        logger.info(
            "KNT SIGNAL PAPER PROBE SUMMARY | decisions=%s submitted=%s actions=%s",
            len(decisions),
            len(submitted),
            {action: sum(1 for d in decisions if d.action == action) for action in sorted({d.action for d in decisions})},
        )

        if executor.last_request is not None:
            req = executor.last_request
            logger.info(
                "KNT PAPER ATTEMPT | symbol=%s strategy=%s side=%s qty=%s entry=%.2f stop=%.2f target=%.2f result=%s",
                req.symbol,
                req.strategy,
                req.side,
                req.quantity,
                req.entry_price,
                req.stop_price,
                req.target_price,
                None if executor.last_result is None else executor.last_result.reason,
            )

        await asyncio.sleep(1.0)
        after = broker.snapshot(account.account)
        logger.warning(
            "KNT SIGNAL PAPER BROKER STATE | positions=%s open_orders=%s details=%s",
            after.position_count,
            after.open_order_count,
            after.nonzero_positions,
        )

        if not submitted:
            logger.warning(
                "KNT SIGNAL PAPER PROBE COMPLETE | no strategy setup passed every gate; no Paper order was intentionally forced"
            )
        else:
            logger.warning(
                "KNT SIGNAL PAPER PROBE COMPLETE | one strategy-originated Paper bracket accepted; inspect broker state before any next execution"
            )
    finally:
        await connection.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
