from __future__ import annotations

from dataclasses import dataclass

from ib_async import LimitOrder, MarketOrder, Order, TagValue


@dataclass(frozen=True)
class BracketTemplate:
    parent: Order
    take_profit: Order
    stop_loss: Order


def _apply_account(order: Order, account: str | None) -> Order:
    if account:
        order.account = account
    return order


def adaptive_entry_order(
    *,
    action: str,
    quantity: float,
    limit_price: float | None = None,
    priority: str = "Normal",
    account: str | None = None,
) -> Order:
    if quantity <= 0:
        raise ValueError("quantity must be > 0")
    if limit_price is None:
        order = MarketOrder(action.upper(), quantity, tif="DAY", outsideRth=False)
    else:
        if limit_price <= 0:
            raise ValueError("limit_price must be > 0")
        order = LimitOrder(action.upper(), quantity, limit_price, tif="DAY", outsideRth=False)
    order.algoStrategy = "Adaptive"
    order.algoParams = [TagValue("adaptivePriority", priority)]
    return _apply_account(order, account)


def bracket_orders(
    *,
    action: str,
    quantity: float,
    entry_price: float,
    take_profit_price: float,
    stop_price: float,
    parent_order_id: int,
    account: str | None = None,
    adaptive_parent: bool = False,
) -> BracketTemplate:
    """Build a server-side parent + take-profit + stop bracket.

    Caller is responsible for allocating a valid parent order id and submitting in
    parent/TP/stop order. The final child transmits the complete bracket atomically.
    """
    if quantity <= 0 or min(entry_price, take_profit_price, stop_price) <= 0:
        raise ValueError("Bracket quantities/prices must be positive")
    action = action.upper()
    if action not in {"BUY", "SELL"}:
        raise ValueError("action must be BUY or SELL")
    exit_action = "SELL" if action == "BUY" else "BUY"

    parent = LimitOrder(action, quantity, entry_price, tif="DAY", outsideRth=False)
    parent.orderId = int(parent_order_id)
    parent.transmit = False
    if adaptive_parent:
        parent.algoStrategy = "Adaptive"
        parent.algoParams = [TagValue("adaptivePriority", "Normal")]

    take_profit = LimitOrder(exit_action, quantity, take_profit_price, tif="GTC", outsideRth=False)
    take_profit.parentId = parent.orderId
    take_profit.transmit = False

    stop_loss = Order(
        action=exit_action,
        orderType="STP",
        totalQuantity=quantity,
        auxPrice=stop_price,
        tif="GTC",
        outsideRth=False,
        parentId=parent.orderId,
        transmit=True,
    )

    for order in (parent, take_profit, stop_loss):
        _apply_account(order, account)
    return BracketTemplate(parent, take_profit, stop_loss)
