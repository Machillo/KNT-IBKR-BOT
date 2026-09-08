from __future__ import annotations

from ib_async import Contract, Trade

from core.order_manager import OrderManager
from core.exceptions import RiskRejectedError
from risk.risk_manager import RiskDecision, RiskManager
from utils.logger import logger


class RiskGatedOrderManager:
    """Normal gateway for opening or increasing exposure."""

    def __init__(self, orders: OrderManager, risk: RiskManager) -> None:
        self.orders = orders
        self.risk = risk

    def _approve(self, *, equity: float, entry_price: float, stop_price: float,
                 quantity: float, symbol: str) -> RiskDecision:
        decision = self.risk.evaluate_trade(
            equity=equity,
            entry_price=entry_price,
            stop_price=stop_price,
            quantity=quantity,
        )
        if not decision.approved:
            logger.warning(
                "RISK GATE REJECTED | symbol=%s qty=%s entry=%s stop=%s "
                "capital_at_risk=%.2f position_value=%.2f reason=%s",
                symbol, quantity, entry_price, stop_price,
                decision.capital_at_risk, decision.position_value, decision.reason,
            )
            raise RiskRejectedError(decision.reason)

        logger.info(
            "RISK GATE APPROVED | symbol=%s qty=%s entry=%s stop=%s "
            "capital_at_risk=%.2f position_value=%.2f",
            symbol, quantity, entry_price, stop_price,
            decision.capital_at_risk, decision.position_value,
        )
        return decision

    def limit_entry(self, contract: Contract, action: str, quantity: float,
                    limit_price: float, *, equity: float, stop_price: float) -> Trade:
        self._approve(
            equity=equity, entry_price=limit_price, stop_price=stop_price,
            quantity=quantity, symbol=contract.localSymbol or contract.symbol,
        )
        return self.orders.limit(contract, action, quantity, limit_price)

    def market_entry(self, contract: Contract, action: str, quantity: float, *,
                     equity: float, reference_price: float, stop_price: float) -> Trade:
        self._approve(
            equity=equity, entry_price=reference_price, stop_price=stop_price,
            quantity=quantity, symbol=contract.localSymbol or contract.symbol,
        )
        return self.orders.market(contract, action, quantity)

    def close_market(self, contract: Contract, action: str, quantity: float) -> Trade:
        """Explicit risk-reducing exit path; not for opening new exposure."""
        logger.warning(
            "RISK-REDUCING EXIT | symbol=%s action=%s qty=%s",
            contract.localSymbol or contract.symbol, action.upper(), quantity,
        )
        return self.orders.market(contract, action, quantity)
