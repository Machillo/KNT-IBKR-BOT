from __future__ import annotations

import asyncio
from dataclasses import dataclass

from ib_async import IB, MarketOrder

from config.config import RiskConfig
from core.broker_state import BrokerStateService
from utils.logger import logger


@dataclass(frozen=True)
class KillSwitchResult:
    triggered: bool
    dry_run: bool
    cancelled_orders: int
    liquidation_orders: int
    flat_confirmed: bool
    reason: str


class KillSwitch:
    """Emergency broker action: block entries elsewhere, cancel working orders, flatten, verify."""

    def __init__(self, ib: IB, settings: RiskConfig, account: str) -> None:
        self.ib = ib
        self.settings = settings
        self.account = account
        self.triggered = False
        self.reason = ""
        self.state = BrokerStateService(ib)
        self._last_result: KillSwitchResult | None = None

    def _account_open_trades(self):
        result = []
        for trade in list(self.ib.openTrades()):
            if trade.isDone():
                continue
            order_account = (getattr(trade.order, "account", "") or "").strip()
            if order_account and order_account != self.account:
                continue
            result.append(trade)
        return result

    def _account_positions(self):
        return [
            p for p in list(self.ib.positions())
            if p.account == self.account and float(p.position) != 0
        ]

    async def execute(self, reason: str, *, dry_run: bool | None = None) -> KillSwitchResult:
        dry_run = self.settings.kill_switch_dry_run if dry_run is None else dry_run
        if self.triggered and self._last_result is not None:
            logger.critical("KILL SWITCH already triggered | duplicate execution suppressed")
            return self._last_result
        self.triggered = True
        self.reason = reason

        if not self.settings.kill_switch_enabled:
            logger.critical("KILL SWITCH required but disabled | reason=%s", reason)
            self._last_result = KillSwitchResult(True, dry_run, 0, 0, False, reason)
            return self._last_result

        working = self._account_open_trades()
        positions = self._account_positions()

        if dry_run:
            logger.critical(
                "KILL SWITCH DRY-RUN | account=%s reason=%s would_cancel=%s would_flatten=%s",
                self.account,
                reason,
                len(working),
                len(positions),
            )
            flat_now = self.state.snapshot(self.account).is_flat
            self._last_result = KillSwitchResult(True, True, 0, 0, flat_now, reason)
            return self._last_result

        logger.critical("KILL SWITCH ARMED | account=%s reason=%s", self.account, reason)

        cancelled = 0
        for trade in working:
            self.ib.cancelOrder(trade.order)
            cancelled += 1

        cancel_deadline = asyncio.get_running_loop().time() + 10
        while self._account_open_trades() and asyncio.get_running_loop().time() < cancel_deadline:
            await asyncio.sleep(0.25)

        liquidation_trades = []
        for position in self._account_positions():
            qty = float(position.position)
            action = "SELL" if qty > 0 else "BUY"
            order = MarketOrder(action, abs(qty), tif="DAY")
            order.account = self.account
            trade = self.ib.placeOrder(position.contract, order)
            liquidation_trades.append(trade)
            logger.critical(
                "KILL SWITCH FLATTEN requested | symbol=%s qty=%s action=%s",
                position.contract.localSymbol or position.contract.symbol,
                abs(qty),
                action,
            )

        if liquidation_trades:
            deadline = asyncio.get_running_loop().time() + 30
            while asyncio.get_running_loop().time() < deadline:
                if all(t.isDone() for t in liquidation_trades):
                    break
                await asyncio.sleep(0.25)

        final_state = await self.state.wait_until_flat(self.account, timeout=10)
        if not final_state.is_flat:
            logger.critical("KILL SWITCH INCOMPLETE | manual intervention required")
        else:
            logger.critical("KILL SWITCH COMPLETE | account=%s broker confirmed flat", self.account)

        self._last_result = KillSwitchResult(
            triggered=True,
            dry_run=False,
            cancelled_orders=cancelled,
            liquidation_orders=len(liquidation_trades),
            flat_confirmed=final_state.is_flat,
            reason=reason,
        )
        return self._last_result
