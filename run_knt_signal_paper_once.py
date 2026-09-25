"""Supervised PAPER plumbing test: at most ONE strategy-originated bracket of at most 1 share.

Three independent confirmations are required (docs/PAPER_PLUMBING_TEST.md):
1. ``KNT_SIGNAL_PAPER_ACK`` set to the literal below for this run (refused if stored in .env);
2. the ``--confirm-paper-plumbing`` command-line flag;
3. an in-process read-only preflight that passes: verified paper account, live two-sided
   quote, flat account (positions and EVERY client's open orders), no persisted lock.
"""
from __future__ import annotations

import argparse
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
from market.session import BrokerCalendarSessionPolicy
from portfolio.state import PortfolioStateService
from utils.logger import logger

ACK = "I_UNDERSTAND_KNT_WILL_SUBMIT_ONE_PAPER_SIGNAL"


class OneShotCappedPaperExecutor:
    """Allow at most one accepted Paper bracket and cap validation quantity.

    The cap is intentionally stricter than portfolio sizing. This runner validates the
    strategy -> admission -> broker path; it is not a sizing validation run.
    """

    def __init__(self, executor: PaperExecutionEngine, *, max_quantity: float = 1.0) -> None:
        import math

        if not math.isfinite(float(max_quantity)) or max_quantity <= 0:
            raise ValueError("max_quantity must be a finite number > 0")
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


async def main(args) -> None:
    from config.config import persisted_env
    from ib_async import Stock

    from config.config import STATE_DIR
    from execution.preflight import MAX_PLUMBING_QTY, broker_checks, config_checks, verdict

    if not args.confirm_paper_plumbing:
        raise RuntimeError("Pass --confirm-paper-plumbing (second confirmation) for this one run")
    if persisted_env().get("KNT_SIGNAL_PAPER_ACK"):
        raise RuntimeError("KNT_SIGNAL_PAPER_ACK must not be stored in .env; set it for this run only")
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

    import math

    max_qty = float(getenv("KNT_SIGNAL_PAPER_MAX_QTY", "1"))
    if not math.isfinite(max_qty) or max_qty <= 0 or max_qty > MAX_PLUMBING_QTY:
        raise RuntimeError(f"KNT_SIGNAL_PAPER_MAX_QTY must be > 0 and <= {MAX_PLUMBING_QTY:g} for plumbing")
    failed_config = [c.name for c in config_checks(config, persisted_env()) if c.required and not c.ok]
    if failed_config:
        raise RuntimeError(f"Paper plumbing preflight (config) failed: {failed_config}")

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

        session_policy = BrokerCalendarSessionPolicy()
        await session_policy.refresh(ib)
        session = session_policy.state()
        if not session.market_open:
            raise RuntimeError(
                f"KNT signal probe requires regular US stock session; state={session.session} "
                f"local={session.local_time.isoformat()}"
            )

        market_data = MarketDataService(ib, config.market_data)
        market_data.configure()
        checks, _ = await broker_checks(ib, config, state_dir=STATE_DIR, market_data=market_data,
                                        probe_contract=Stock("SPY", "SMART", "USD"), session_policy=session_policy)
        result, failed = verdict(checks)
        if failed:
            raise RuntimeError(f"Paper plumbing preflight (broker) failed: {failed}")
        logger.warning("KNT PAPER PLUMBING PREFLIGHT | %s", result)
        intelligence = MarketIntelligenceService(ib, market_data)
        portfolio_state = PortfolioStateService(ib).build(
            account,
            starting_equity=context.starting_equity,
            daily_loss_limit_pct=config.risk.max_daily_loss_pct,
            trading_locked=context.risk.trading_locked,
        )

        guard = supervisor.paper_guard
        if guard is None or not guard.verification.verified:
            reason = "missing" if guard is None else guard.verification.reason
            raise RuntimeError(f"KNT signal probe requires a verified PAPER account; reason={reason}")
        raw_executor = PaperExecutionEngine(
            ib,
            account=account.account,
            enabled=True,
            paper_guard=guard,
            risk_manager=context.risk,
            session_policy=session_policy,
            max_entries_per_day=1,
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
            session_policy=session_policy,
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
            "KNT SIGNAL PAPER BROKER STATE | positions=%s open_orders=%s",
            after.position_count,
            after.open_order_count,
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-paper-plumbing", action="store_true")
    asyncio.run(main(parser.parse_args()))
