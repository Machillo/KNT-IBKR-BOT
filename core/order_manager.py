from __future__ import annotations

import asyncio
from typing import Iterable

from ib_async import Contract, IB, LimitOrder, MarketOrder, Trade

from core.order_templates import adaptive_entry_order, bracket_orders
from utils.logger import logger


class OrderManager:
    """Broker execution layer. Strategies must pass risk checks before using it."""

    def __init__(self, ib: IB, account: str | None = None) -> None:
        self.ib = ib
        self.account = account
        self._callback_order_ids: set[int] = set()

    def _attach_trade_callbacks(self, trade: Trade) -> Trade:
        order_id = trade.order.orderId
        if order_id in self._callback_order_ids:
            return trade
        self._callback_order_ids.add(order_id)

        def on_status(updated_trade: Trade) -> None:
            logger.info(
                "ORDER STATUS | id=%s status=%s filled=%s remaining=%s avg_fill=%s",
                updated_trade.order.orderId,
                updated_trade.orderStatus.status,
                updated_trade.orderStatus.filled,
                updated_trade.orderStatus.remaining,
                updated_trade.orderStatus.avgFillPrice,
            )

        def on_fill(updated_trade: Trade, fill) -> None:
            logger.info(
                "ORDER FILL | id=%s symbol=%s shares=%s price=%s exec_id=%s",
                updated_trade.order.orderId,
                fill.contract.localSymbol or fill.contract.symbol,
                fill.execution.shares,
                fill.execution.price,
                fill.execution.execId,
            )

        trade.statusEvent += on_status
        trade.fillEvent += on_fill
        return trade

    def market(self, contract: Contract, action: str, quantity: float) -> Trade:
        order = MarketOrder(action.upper(), quantity, tif="DAY", outsideRth=False)
        if self.account:
            order.account = self.account
        trade = self.ib.placeOrder(contract, order)
        logger.info("MARKET ORDER submitted | id=%s action=%s qty=%s tif=%s",
                    trade.order.orderId, action.upper(), quantity, trade.order.tif)
        return self._attach_trade_callbacks(trade)

    def adaptive(
        self,
        contract: Contract,
        action: str,
        quantity: float,
        *,
        limit_price: float | None = None,
        priority: str = "Normal",
    ) -> Trade:
        order = adaptive_entry_order(
            action=action,
            quantity=quantity,
            limit_price=limit_price,
            priority=priority,
            account=self.account,
        )
        trade = self.ib.placeOrder(contract, order)
        logger.info(
            "ADAPTIVE ORDER submitted | id=%s action=%s qty=%s type=%s priority=%s",
            trade.order.orderId, action.upper(), quantity, trade.order.orderType, priority,
        )
        return self._attach_trade_callbacks(trade)

    def bracket_limit(
        self,
        contract: Contract,
        action: str,
        quantity: float,
        *,
        entry_price: float,
        take_profit_price: float,
        stop_price: float,
        adaptive_parent: bool = True,
    ) -> tuple[Trade, Trade, Trade]:
        parent_id = int(self.ib.client.getReqId())
        template = bracket_orders(
            action=action,
            quantity=quantity,
            entry_price=entry_price,
            take_profit_price=take_profit_price,
            stop_price=stop_price,
            parent_order_id=parent_id,
            account=self.account,
            adaptive_parent=adaptive_parent,
        )
        trades = tuple(
            self._attach_trade_callbacks(self.ib.placeOrder(contract, order))
            for order in (template.parent, template.take_profit, template.stop_loss)
        )
        logger.info(
            "BRACKET ORDER submitted | parent=%s action=%s qty=%s entry=%s target=%s stop=%s adaptive=%s",
            parent_id, action.upper(), quantity, entry_price, take_profit_price, stop_price, adaptive_parent,
        )
        return trades

    def limit(self, contract: Contract, action: str, quantity: float, limit_price: float) -> Trade:
        order = LimitOrder(action.upper(), quantity, limit_price, tif="DAY", outsideRth=False)
        if self.account:
            order.account = self.account
        trade = self.ib.placeOrder(contract, order)
        logger.info("LIMIT ORDER submitted | id=%s action=%s qty=%s limit=%s tif=%s",
                    trade.order.orderId, action.upper(), quantity, limit_price, trade.order.tif)
        return self._attach_trade_callbacks(trade)

    def modify(self, contract: Contract, trade: Trade, *, quantity: float | None = None,
               limit_price: float | None = None) -> Trade:
        order = trade.order
        if trade.isDone():
            raise RuntimeError(f"Cannot modify completed order {order.orderId}")
        if quantity is not None:
            order.totalQuantity = quantity
        if limit_price is not None:
            order.lmtPrice = limit_price
        updated_trade = self.ib.placeOrder(contract, order)
        logger.info("ORDER MODIFICATION requested | id=%s qty=%s limit=%s tif=%s",
                    order.orderId, order.totalQuantity, getattr(order, "lmtPrice", None), order.tif)
        return self._attach_trade_callbacks(updated_trade)

    def cancel(self, trade: Trade) -> Trade | None:
        if trade.isDone():
            logger.info("Cancellation skipped; order already done | id=%s status=%s",
                        trade.order.orderId, trade.orderStatus.status)
            return trade
        logger.info("ORDER CANCELLATION requested | id=%s", trade.order.orderId)
        return self.ib.cancelOrder(trade.order)

    async def wait_for_status(self, trade: Trade, statuses: Iterable[str], timeout: float = 10.0) -> str:
        expected = set(statuses)
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            status = trade.orderStatus.status
            if status in expected:
                return status
            await asyncio.sleep(0.05)
        raise TimeoutError(
            f"Order {trade.order.orderId} did not reach {sorted(expected)}; last status={trade.orderStatus.status!r}"
        )

    async def wait_until_filled(self, trade: Trade, timeout: float = 15.0) -> Trade:
        await self.wait_for_status(trade, {"Filled"}, timeout=timeout)
        if trade.orderStatus.filled <= 0:
            raise RuntimeError(f"Order {trade.order.orderId} reported Filled with no filled quantity")
        return trade

    def cancel_all_open_orders(self) -> None:
        for trade in list(self.ib.openTrades()):
            if not trade.isDone():
                self.ib.cancelOrder(trade.order)
