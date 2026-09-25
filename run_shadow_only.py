"""Order-incapable shadow mode: accumulate point-in-time evidence without any executor.

* Read-only IBKR API session with its own clientId (``read_only_ibkr_settings``).
* No paper executor is created; the shadow engine can never submit anything.
* Kill switch forced to DRY-RUN for this process regardless of ``.env``.
* Uses the real supervisor (account/risk state), discovery, DecisionPipeline and portfolio
  admission, and journals every cycle: discovery funnel + every decision (incl. NO_TRADE).
* Selector learning is FROZEN (no evidence bonus/AVOID, no research scheduler) and approved
  setups go through the executor's pure pre-trade + hard-risk checks with a fresh read-only
  quote: journaled as SHADOW_SUBMIT or SHADOW_BLOCKED:<reason> (nothing is ever sent).
* ``--cycles N`` stops after N cycles (0 = until Ctrl+C). Score later with run_score_shadow.py.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
from pathlib import Path

from config.config import STATE_DIR, BotConfig, config, read_only_ibkr_settings


def research_locked(supervisor, equity: float | None, real_state_dir: Path | None = None) -> str | None:
    """Reason the research decisions must be treated as locked, or None.

    Computed INDEPENDENTLY from each source instead of from ``RiskManager.lock_reason``:
    in this read-only process the paper-verification lock (expected: a readonly session is
    never verified as paper) is applied first and would mask any later lock's reason.
    Sources: this process's sticky daily kill / daily-loss limit / multi-day drawdown lock
    (state/shadow_only), the TRADING BOT's persisted sticky kill and drawdown lock in
    ``real_state_dir`` (read-only, never written), and an unavailable equity reading.
    Anything unreadable fails closed.
    """
    context = supervisor.context
    if context is None or equity is None or equity <= 0:
        return "equity_unavailable"
    if real_state_dir is not None:
        reason = _real_bot_lock(Path(real_state_dir), context.account, context.trading_date)
        if reason:
            return reason
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


def _real_bot_lock(state_dir: Path, account: str, trading_date: str) -> str | None:
    from risk.drawdown_guard import DrawdownStateStore
    from risk.state_store import DailyRiskStateStore

    try:
        daily = DailyRiskStateStore(state_dir / "risk_state.json").get(account=account, trading_date=trading_date)
        drawdown = DrawdownStateStore(state_dir / "drawdown_state.json").get(account)
    except Exception:
        return "bot_state_unreadable"
    if daily is not None and daily.kill_switch_triggered:
        return "bot_sticky_daily_kill"
    if drawdown is not None and drawdown.locked:
        return "bot_multi_day_drawdown"
    return None


def shadow_only_config(base: BotConfig) -> BotConfig:
    """Same runtime settings, but a read-only session, own clientId, dry-run kill switch,
    autonomous trading disabled and live trading refused — nothing in this process can send an
    order, and it never even connects to a live port (``validate`` raises on one)."""
    ibkr = read_only_ibkr_settings(replace(base.ibkr, allow_live_trading=False), 53)
    if ibkr.client_id <= 0:
        # clientId 0 would bind TWS manual orders (reqAutoOpenOrders); refuse it outright.
        raise RuntimeError("shadow-only clientId must be > 0")
    return replace(
        base,
        ibkr=ibkr,
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
            state_dir=STATE_DIR / "shadow_only",
            # Forward evidence: frozen selector (no learning drift inside the window) and the
            # order-free execution model (pretrade + hard risk + daily cap + virtual book).
            learning_enabled=False, research_execution=True, run_mode="shadow_only",
        )
        states = PortfolioStateService(ib)
        logger.info("SHADOW-ONLY running | account=%s readonly=True executor=None", mask_account(account.account))
        cycle = 0
        while args.cycles == 0 or cycle < args.cycles:
            try:
                await supervisor.evaluate()
                snapshot = await supervisor.accounts.snapshot(context.account, log=False)
                reason = research_locked(supervisor, snapshot.net_liquidation, real_state_dir=STATE_DIR)
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
