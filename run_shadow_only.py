"""Order-incapable shadow mode: accumulate point-in-time evidence without any executor.

* Read-only IBKR API session with its own clientId (``read_only_ibkr_settings``).
* No paper executor is created; the shadow engine can never submit anything.
* Kill switch forced to DRY-RUN for this process regardless of ``.env``.
* Uses the real supervisor (account/risk state), discovery, DecisionPipeline and portfolio
  admission, and journals every cycle: discovery funnel + every decision (incl. NO_TRADE).
* ``--cycles N`` stops after N cycles (0 = until Ctrl+C). Score later with run_score_shadow.py.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace

from config.config import STATE_DIR, BotConfig, config, read_only_ibkr_settings


EXPECTED_READONLY_LOCK = "paper_account_unverified:readonly_session"


def research_lock(risk) -> bool:
    """Lock state used for research decisions. The read-only session is (correctly) never
    verified as paper, which locks entries; in this order-incapable process that lock is
    expected and would otherwise turn every decision into 'allocation_unavailable'. Any
    other lock (daily loss, drawdown, monitoring error) is kept."""
    return bool(risk.trading_locked) and str(risk.lock_reason) != EXPECTED_READONLY_LOCK


def shadow_only_config(base: BotConfig) -> BotConfig:
    """Same runtime settings, but a read-only session, own clientId, dry-run kill switch and
    autonomous trading disabled — nothing in this process can send an order."""
    return replace(
        base,
        ibkr=read_only_ibkr_settings(base.ibkr, 53),
        risk=replace(base.risk, kill_switch_dry_run=True),
        runtime=replace(base.runtime, autonomous_trading_enabled=False),
    )


async def main_async(args) -> None:
    from core.connection import IBKRConnection
    from core.market_data import MarketDataService
    from core.paper_guard import mask_account
    from engine.shadow import ShadowTradingEngine
    from engine.supervisor import PaperSupervisor
    from market.intelligence import MarketIntelligenceService
    from market.session import BrokerCalendarSessionPolicy
    from portfolio.state import PortfolioStateService
    from utils.logger import logger

    cfg = shadow_only_config(config)
    cfg.validate()
    connection = IBKRConnection(cfg.ibkr)
    try:
        ib = await connection.connect()
        # Own state directory: shadow-only never races the trading bot's persisted locks.
        supervisor = PaperSupervisor(ib, cfg, state_dir=STATE_DIR / "shadow_only")
        context, account = await supervisor.initialize()
        intelligence = MarketIntelligenceService(ib, MarketDataService(ib, cfg.market_data))
        shadow = ShadowTradingEngine(
            ib, intelligence, max_candidates=cfg.runtime.shadow_max_candidates,
            quote_budget=cfg.runtime.universe_quote_budget, risk_manager=context.risk,
            paper_executor=None, session_policy=BrokerCalendarSessionPolicy(),
        )
        states = PortfolioStateService(ib)
        logger.info("SHADOW-ONLY running | account=%s readonly=True executor=None", mask_account(account.account))
        cycle = 0
        while args.cycles == 0 or cycle < args.cycles:
            try:
                await supervisor.evaluate()
                snapshot = await supervisor.accounts.snapshot(context.account, log=False)
                state = states.build(snapshot, starting_equity=context.starting_equity,
                                     daily_loss_limit_pct=cfg.risk.max_daily_loss_pct,
                                     trading_locked=research_lock(context.risk))
                await shadow.run_once(cfg.runtime.discovery_rows, portfolio_state=state)
            except Exception as exc:
                logger.exception("SHADOW-ONLY cycle failed | error=%s", exc)
            cycle += 1
            if args.cycles and cycle >= args.cycles:
                break
            await asyncio.sleep(cfg.runtime.shadow_interval_seconds)
    finally:
        await connection.disconnect()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cycles", type=int, default=0)
    asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    main()
