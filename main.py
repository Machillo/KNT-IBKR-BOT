from __future__ import annotations

import asyncio
import signal
from datetime import datetime
from zoneinfo import ZoneInfo

from ib_async import Stock

from config.config import config
from core.broker_state import BrokerStateService
from core.connection import IBKRConnection
from core.market_data import MarketDataService
from core.order_manager import OrderManager
from engine.supervisor import PaperSupervisor
from market.intelligence import MarketIntelligenceService
from risk.order_gate import RiskGatedOrderManager
from risk.risk_manager import RiskManager
from utils.logger import logger


async def qualify_spy(ib):
    qualified = await ib.qualifyContractsAsync(Stock("SPY", "SMART", "USD"))
    if not qualified:
        raise RuntimeError("SPY contract could not be qualified")
    return qualified[0]


async def is_in_liquid_hours(ib, contract) -> bool:
    details = await ib.reqContractDetailsAsync(contract)
    if not details:
        logger.warning("BROKER SMOKE TEST skipped: contract details unavailable")
        return False

    detail = details[0]
    liquid_hours = (detail.liquidHours or "").strip()
    timezone_id = (detail.timeZoneId or "America/New_York").strip()
    try:
        tz = ZoneInfo(timezone_id)
    except Exception:
        tz = ZoneInfo("America/New_York")

    now = datetime.now(tz)
    today = now.strftime("%Y%m%d")
    for session in liquid_hours.split(";"):
        session = session.strip()
        if not session or session.endswith(":CLOSED") or "-" not in session:
            continue
        start_raw, end_raw = session.split("-", 1)
        try:
            start = datetime.strptime(start_raw, "%Y%m%d:%H%M").replace(tzinfo=tz)
            end = datetime.strptime(end_raw, "%Y%m%d:%H%M").replace(tzinfo=tz)
        except ValueError:
            continue
        if start.strftime("%Y%m%d") == today and start <= now <= end:
            return True

    logger.info(
        "BROKER SMOKE TEST skipped: SPY outside liquid hours | now=%s timezone=%s",
        now.isoformat(timespec="seconds"),
        timezone_id,
    )
    return False


async def run_broker_smoke_tests(ib, market_data: MarketDataService, equity: float,
                                 risk: RiskManager, account: str) -> None:
    """Paper-only end-to-end execution test. Must be explicitly enabled."""
    if config.ibkr.readonly:
        logger.warning("BROKER SMOKE TEST skipped because IBKR_READONLY=true")
        return

    contract = await qualify_spy(ib)
    if not await is_in_liquid_hours(ib, contract):
        return

    market_data.configure()
    snapshot = await market_data.test_stock_snapshot("SPY")
    reference = snapshot.ask or snapshot.market_price or snapshot.last or snapshot.bid
    if reference is None:
        logger.warning("BROKER SMOKE TEST skipped: no usable SPY reference price")
        return

    orders = OrderManager(ib, account)
    gated = RiskGatedOrderManager(orders, risk)
    stop_price = round(reference * 0.95, 2)

    logger.warning("BROKER SMOKE TEST | BUY 1 SPY -> SELL 1 SPY -> broker reconciliation")
    buy = gated.market_entry(
        contract,
        "BUY",
        1,
        equity=equity,
        reference_price=reference,
        stop_price=stop_price,
    )
    await orders.wait_until_filled(buy, timeout=15)
    logger.info("BROKER SMOKE BUY FILLED | avg_fill=%s", buy.orderStatus.avgFillPrice)

    sell = gated.close_market(contract, "SELL", 1)
    await orders.wait_until_filled(sell, timeout=15)
    logger.info("BROKER SMOKE SELL FILLED | avg_fill=%s", sell.orderStatus.avgFillPrice)

    reconciled = await BrokerStateService(ib).wait_until_flat(account, timeout=15)
    if not reconciled.is_flat:
        risk.lock_trading("Broker smoke test did not reconcile to flat")
        raise RuntimeError("BROKER SMOKE TEST failed: broker did not confirm flat state")
    logger.info("BROKER SMOKE TEST COMPLETE | broker confirmed position=0 open_orders=0")


async def run_discovery_probe(ib, market_data: MarketDataService) -> None:
    """Read-only discovery/intelligence probe; does not place orders."""
    ranked = await MarketIntelligenceService(ib, market_data).ranked_us_stocks(
        config.runtime.discovery_rows
    )
    eligible = [item for item in ranked if item.eligible]
    if not eligible:
        logger.warning("DISCOVERY PROBE completed but found no eligible candidates")
        return
    logger.info(
        "DISCOVERY PROBE COMPLETE | top=%s score=%.2f count=%s",
        eligible[0].symbol,
        eligible[0].score,
        len(eligible),
    )


async def main() -> None:
    config.validate()
    connection = IBKRConnection(config.ibkr)
    stop_event = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    supervisor_task: asyncio.Task | None = None
    try:
        ib = await connection.connect()
        market_data = MarketDataService(ib, config.market_data)
        supervisor = PaperSupervisor(ib, config)
        context, account = await supervisor.initialize()

        logger.info(
            "KNT MODE | paper=%s kill_switch=%s autonomous_trading=%s smoke_tests=%s discovery_probe=%s",
            config.ibkr.port in config.ibkr.paper_ports,
            "DRY_RUN" if config.risk.kill_switch_dry_run else "ARMED",
            config.runtime.autonomous_trading_enabled,
            config.runtime.run_broker_smoke_tests,
            config.runtime.run_discovery_probe,
        )

        if config.runtime.run_discovery_probe:
            await run_discovery_probe(ib, market_data)

        if config.runtime.run_broker_smoke_tests:
            if context.risk.trading_locked:
                logger.critical("BROKER SMOKE TEST blocked | reason=%s", context.risk.lock_reason)
            else:
                await run_broker_smoke_tests(
                    ib,
                    market_data,
                    account.net_liquidation,
                    context.risk,
                    account.account,
                )

        if config.runtime.autonomous_trading_enabled:
            logger.warning(
                "AUTONOMOUS_TRADING_ENABLED=true but no production strategy is armed yet; "
                "supervisor/discovery will run without opening strategy positions"
            )

        logger.info("KNT PAPER AUTONOMOUS CORE running | Ctrl+C to stop")
        supervisor_task = asyncio.create_task(supervisor.run(stop_event))
        await stop_event.wait()
    finally:
        stop_event.set()
        if supervisor_task is not None:
            try:
                await asyncio.wait_for(supervisor_task, timeout=2)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                supervisor_task.cancel()
        await connection.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
