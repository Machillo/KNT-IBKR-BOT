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


def research_locked(supervisor, equity: float | None) -> str | None:
    """Reason the research decisions must be treated as locked, or None.

    Computed INDEPENDENTLY from each real source instead of from ``RiskManager.lock_reason``:
    in this read-only process the paper-verification lock (expected: a readonly session is
    never verified as paper) is applied first and would mask any later lock's reason.
    Sources: persisted sticky daily kill, daily-loss limit, multi-day drawdown lock, and an
    unavailable/invalid equity reading (fail closed).
    """
    context = supervisor.context
    if context is None or equity is None or equity <= 0:
        return "equity_unavailable"
    if getattr(supervisor, "_kill_persisted", False):
        return "sticky_daily_kill"
    daily = context.risk.daily_state(starting_equity=context.starting_equity, current_equity=equity)
    if daily.kill_switch_required:
        return "daily_loss_limit"
    drawdown = getattr(supervisor, "drawdown", None)
    if drawdown is not None:
        try:
            record = drawdown.store.get(context.account)
        except Exception:
            return "drawdown_state_unavailable"
        if record is not None and record.locked:
            return "multi_day_drawdown"
    return None


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
    from risk.risk_manager import RiskManager
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
        # Research copy of the hard risk manager: same limits, mirrors every real lock except the
        # expected readonly-session lock (see research_lock).
        research_risk = RiskManager(cfg.risk)
        shadow = ShadowTradingEngine(
            ib, intelligence, max_candidates=cfg.runtime.shadow_max_candidates,
            quote_budget=cfg.runtime.universe_quote_budget, risk_manager=research_risk,
            paper_executor=None, session_policy=BrokerCalendarSessionPolicy(),
        )
        states = PortfolioStateService(ib)
        logger.info("SHADOW-ONLY running | account=%s readonly=True executor=None", mask_account(account.account))
        cycle = 0
        while args.cycles == 0 or cycle < args.cycles:
            try:
                await supervisor.evaluate()
                snapshot = await supervisor.accounts.snapshot(context.account, log=False)
                reason = research_locked(supervisor, snapshot.net_liquidation)
                if reason:
                    research_risk.lock_trading(reason)
                else:
                    research_risk.trading_locked, research_risk.lock_reason = False, ""
                state = states.build(snapshot, starting_equity=context.starting_equity,
                                     daily_loss_limit_pct=cfg.risk.max_daily_loss_pct,
                                     trading_locked=research_risk.trading_locked)
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
