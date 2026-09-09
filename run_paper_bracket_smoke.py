from __future__ import annotations

import asyncio
from os import getenv

from ib_async import Stock

from config.config import config
from core.connection import IBKRConnection
from core.market_data import MarketDataService
from core.order_manager import OrderManager
from utils.logger import logger

ACK = "I_UNDERSTAND_THIS_SUBMITS_A_PAPER_ORDER"


async def main() -> None:
    config.validate()
    if config.ibkr.port not in config.ibkr.paper_ports:
        raise RuntimeError(f"Smoke test requires an IBKR Paper port; got {config.ibkr.port}")
    if config.ibkr.allow_live_trading:
        raise RuntimeError("Set ALLOW_LIVE_TRADING=false before running the Paper smoke test")
    if getenv("PAPER_SMOKE_ACK", "") != ACK:
        raise RuntimeError(
            "Paper smoke test is armed but not authorized. Set "
            f"PAPER_SMOKE_ACK={ACK} for this one run."
        )

    symbol = getenv("PAPER_SMOKE_SYMBOL", "SPY").strip().upper() or "SPY"
    connection = IBKRConnection(config.ibkr)
    try:
        ib = await connection.connect()
        accounts = list(ib.managedAccounts())
        if not accounts:
            raise RuntimeError("IBKR returned no managed Paper accounts")
        account = config.ibkr.account or accounts[0]

        contract = Stock(symbol, "SMART", "USD")
        qualified = await ib.qualifyContractsAsync(contract)
        if not qualified:
            raise RuntimeError(f"Could not qualify {symbol}")
        contract = qualified[0]

        market_data = MarketDataService(ib, config.market_data)
        market_data.configure()
        snapshot = await market_data.snapshot_contract(contract, symbol=symbol, timeout=8.0)
        market_price = float(snapshot.market_price or snapshot.last or snapshot.bid or snapshot.ask or 0.0)
        if market_price <= 0:
            raise RuntimeError(f"No usable Paper/delayed market price for {symbol}")

        # Deliberately place a 1-share BUY well below market so the parent should not fill.
        entry = round(max(0.01, market_price * 0.50), 2)
        target = round(entry * 1.05, 2)
        stop = round(max(0.01, entry * 0.95), 2)

        logger.info(
            "PAPER BRACKET SMOKE | account=%s symbol=%s market=%.2f qty=1 entry=%.2f target=%.2f stop=%.2f",
            account, symbol, market_price, entry, target, stop,
        )

        orders = OrderManager(ib, account=account)
        trades = orders.bracket_limit(
            contract,
            "BUY",
            1,
            entry_price=entry,
            take_profit_price=target,
            stop_price=stop,
            adaptive_parent=True,
        )
        await asyncio.sleep(2.0)

        if len(trades) != 3:
            raise RuntimeError(f"Expected 3 bracket legs; got {len(trades)}")
        parent, take_profit, stop_loss = trades
        logger.info(
            "PAPER BRACKET ACCEPTED | parent=%s:%s tp=%s:%s stop=%s:%s",
            parent.order.orderId, parent.orderStatus.status,
            take_profit.order.orderId, take_profit.orderStatus.status,
            stop_loss.order.orderId, stop_loss.orderStatus.status,
        )

        if float(parent.orderStatus.filled or 0.0) > 0:
            raise RuntimeError(
                "Smoke-test parent unexpectedly filled. Stop here and inspect the Paper account before rerunning."
            )

        for trade in reversed(trades):
            if not trade.isDone():
                orders.cancel(trade)
        await asyncio.sleep(2.0)

        logger.info(
            "PAPER BRACKET CLEANUP | parent=%s:%s tp=%s:%s stop=%s:%s",
            parent.order.orderId, parent.orderStatus.status,
            take_profit.order.orderId, take_profit.orderStatus.status,
            stop_loss.order.orderId, stop_loss.orderStatus.status,
        )
        logger.info("PAPER BRACKET SMOKE PASSED | no autonomous trading was enabled")
    finally:
        await connection.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
