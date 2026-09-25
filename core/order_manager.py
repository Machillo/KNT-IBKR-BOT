from __future__ import annotations

import asyncio
from typing import Iterable

from ib_async import Contract, IB, LimitOrder, MarketOrder, TagValue, Trade

from core.order_templates import adaptive_entry_order
from core.paper_guard import PaperGuardError, PaperOrderGuard
from utils.logger import logger


class OrderManager:
    """Broker execution layer. Strategies must pass risk checks before using it.

    Every transmission goes through ``_transmit``, which requires a
    ``PaperOrderGuard`` that re-verifies the paper session immediately before
    ``ib.placeOrder``. Without a guard nothing is ever sent (fail closed).
    """

    def __init__(self, ib: IB, account: str | None = None, *, guard: PaperOrderGuard | None = None) -> None:
        self.ib = ib
        self.guard = guard
        if guard is not None and account and account != guard.account:
            raise PaperGuardError("OrderManager account differs from the verified paper account")
        self.account = guard.account if guard is not None else account
        self._callback_order_ids: set[int] = set()

    def _assert_session(self) -> None:
        """Cancels also need a verified paper session: never touch an unverified account."""
        if self.guard is None:
            raise PaperGuardError("No paper guard configured; broker action refused")
        probe = type("_Probe", (), {"account": self.guard.account})()
        self.guard.assert_can_transmit(probe)

    def _transmit(self, contract: Contract, order) -> Trade:
        if self.guard is None:
            raise PaperGuardError("No paper guard configured; order transmission refused")
        if not getattr(order, "account", ""):
            order.account = self.guard.account
        self.guard.assert_can_transmit(order)
        return self.ib.placeOrder(contract, order)

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
        trade = self._transmit(contract, order)
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
        trade = self._transmit(contract, order)
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
        bracket = self.ib.bracketOrder(
            action.upper(), quantity, entry_price, take_profit_price, stop_price,
            tif="DAY", outsideRth=False,
        )
        if adaptive_parent:
            bracket.parent.algoStrategy = "Adaptive"
            bracket.parent.algoParams = [TagValue("adaptivePriority", "Normal")]
        for order in bracket:
            if self.account:
                order.account = self.account
        # Validate every leg before transmitting any of them, so a guard refusal can
        # never leave a parent without its protective children.
        if self.guard is None:
            raise PaperGuardError("No paper guard configured; order transmission refused")
        for order in bracket:
            self.guard.assert_can_transmit(order)
        trades = tuple(
            self._attach_trade_callbacks(self._transmit(contract, order))
            for order in bracket
        )
        logger.info(
            "BRACKET ORDER submitted | parent=%s action=%s qty=%s entry=%s target=%s stop=%s adaptive=%s",
            bracket.parent.orderId, action.upper(), quantity, entry_price,
            take_profit_price, stop_price, adaptive_parent,
        )
        return trades

    def limit(self, contract: Contract, action: str, quantity: float, limit_price: float) -> Trade:
        order = LimitOrder(action.upper(), quantity, limit_price, tif="DAY", outsideRth=False)
        if self.account:
            order.account = self.account
        trade = self._transmit(contract, order)
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
        updated_trade = self._transmit(contract, order)
        logger.info("ORDER MODIFICATION requested | id=%s qty=%s limit=%s tif=%s",
                    order.orderId, order.totalQuantity, getattr(order, "lmtPrice", None), order.tif)
        return self._attach_trade_callbacks(updated_trade)

    def cancel(self, trade: Trade) -> Trade | None:
        if trade.isDone():
            logger.info("Cancellation skipped; order already done | id=%s status=%s",
                        trade.order.orderId, trade.orderStatus.status)
            return trade
        self._assert_session()
        order_account = (getattr(trade.order, "account", "") or "").strip()
        if order_account and order_account != self.account:
            raise PaperGuardError("Refusing to cancel an order of another account")
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
        """Cancel this manager's account orders only; never another account's orders."""
        if not self.account:
            raise PaperGuardError("cancel_all_open_orders requires an explicit account")
        self._assert_session()
        for trade in list(self.ib.openTrades()):
            if trade.isDone():
                continue
            if (getattr(trade.order, "account", "") or "").strip() != self.account:
                continue
            self.ib.cancelOrder(trade.order)
