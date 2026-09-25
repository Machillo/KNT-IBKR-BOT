from __future__ import annotations

import asyncio
from dataclasses import dataclass

from ib_async import IB, MarketOrder

from config.config import RiskConfig
from core.broker_state import BrokerStateService
from core.paper_guard import PaperGuardError, PaperOrderGuard, mask_account
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
    """Emergency broker action: block entries elsewhere, cancel working orders, flatten, verify.

    The ARMED path only acts on a session verified as PAPER by ``PaperOrderGuard``.
    On an unverified session it touches nothing: cancelling or liquidating a
    possibly-live account the bot never traded would destroy human positions.
    """

    def __init__(self, ib: IB, settings: RiskConfig, account: str,
                 guard: PaperOrderGuard | None = None,
                 liquidation_qty_limit: float | None = None) -> None:
        # Drills pass a tiny limit: a position larger than this is NOT liquidated (re-checked
        # at liquidation time, after cancels, so a late fill cannot be flattened at size).
        self.liquidation_qty_limit = liquidation_qty_limit
        self.ib = ib
        self.settings = settings
        self.account = account
        self.guard = guard
        self.triggered = False
        self.reason = ""
        self.state = BrokerStateService(ib)
        self._last_result: KillSwitchResult | None = None

    def _own_client_id(self) -> int | None:
        client = getattr(self.ib, "client", None)
        value = getattr(client, "clientId", None)
        return value if isinstance(value, int) else None

    def _account_open_trades(self):
        """Working orders of the account that THIS client may cancel. Orders of other clients
        (which ib_async may cache after a reqAllOpenOrders) are excluded: cancelOrder uses the
        orderId, which is namespaced per clientId, so cancelling a foreign order could hit one of
        our own orders with the same number."""
        own = self._own_client_id()
        result = []
        for trade in list(self.ib.openTrades()):
            if trade.isDone():
                continue
            order_account = (getattr(trade.order, "account", "") or "").strip()
            if order_account and order_account != self.account:
                continue
            order_client = getattr(trade.order, "clientId", None)
            if own is not None and isinstance(order_client, int) and order_client != own:
                continue
            result.append(trade)
        return result

    async def _all_open_trades(self):
        """Working orders of the account from EVERY client (read-only request, used only as a
        VETO). Returns None when the broker cannot answer: unknown is never treated as none."""
        request = getattr(self.ib, "reqAllOpenOrdersAsync", None)
        if request is None:
            return None
        try:
            trades = await asyncio.wait_for(request(), timeout=5)
        except Exception:
            return None
        result = []
        for trade in list(trades or []):
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

    def _guard_refusal(self) -> str | None:
        if self.guard is None:
            return "no_paper_guard"
        if self.guard.account != self.account:
            return "guard_account_mismatch"
        probe = type("_Probe", (), {"account": self.account})()
        try:
            self.guard.assert_can_transmit(probe)
        except PaperGuardError as exc:
            return str(exc)
        return None

    async def execute(self, reason: str, *, dry_run: bool | None = None) -> KillSwitchResult:
        dry_run = self.settings.kill_switch_dry_run if dry_run is None else dry_run
        if self.triggered and self._last_result is not None and (
                self._last_result.flat_confirmed or self._last_result.dry_run):
            logger.critical("KILL SWITCH already triggered | duplicate execution suppressed")
            return self._last_result
        # An armed run that did not confirm flat may run again (e.g. a cancel confirmed late).
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
                mask_account(self.account),
                reason,
                len(working),
                len(positions),
            )
            flat_now = self.state.snapshot(self.account).is_flat
            self._last_result = KillSwitchResult(True, True, 0, 0, flat_now, reason)
            return self._last_result

        guard_reason = self._guard_refusal()
        if guard_reason is not None:
            logger.critical(
                "KILL SWITCH ARMED but session not verified as PAPER (%s) | no broker action taken; "
                "manual intervention required", guard_reason,
            )
            self._last_result = KillSwitchResult(True, False, 0, 0, False, reason)
            return self._last_result

        logger.critical("KILL SWITCH ARMED | account=%s reason=%s", mask_account(self.account), reason)

        # Positions KNT may flatten automatically: whole-share stocks within the drill limit. Every
        # other position is left to a human WITH its protective orders (cancelling a stop on a
        # position we will not flatten would leave it naked).
        def con_id(obj) -> int:
            return int(getattr(getattr(obj, "contract", None), "conId", 0) or 0)

        flattenable, manual_con_ids = [], set()
        for position in positions:
            qty = float(position.position)
            sec_type = str(getattr(position.contract, "secType", "") or "").upper()
            if sec_type != "STK" or qty != int(qty):
                logger.critical("KILL SWITCH FLATTEN skipped | non-stock or fractional position | "
                                "manual intervention required")
                manual_con_ids.add(con_id(position))
            elif self.liquidation_qty_limit is not None and abs(qty) > self.liquidation_qty_limit:
                logger.critical("KILL SWITCH FLATTEN skipped | position exceeds liquidation limit | "
                                "manual intervention required")
                manual_con_ids.add(con_id(position))
            else:
                flattenable.append(position)

        cancelled = 0
        for trade in working:
            if con_id(trade) and con_id(trade) in manual_con_ids:
                continue
            self.ib.cancelOrder(trade.order)
            cancelled += 1

        cancel_deadline = asyncio.get_running_loop().time() + 10
        while self._account_open_trades() and asyncio.get_running_loop().time() < cancel_deadline:
            # Only orders KNOWN to belong to positions left to a human may remain (conId 0 = unknown).
            if all(con_id(t) and con_id(t) in manual_con_ids for t in self._account_open_trades()):
                break
            await asyncio.sleep(0.25)

        # Every client's working orders (this clientId cannot see or cancel another client's
        # GTC stop). A live order on a position means a market flatten could later be followed
        # by that order firing and REVERSING the position: leave it to a human.
        all_orders = await self._all_open_trades()
        if all_orders is None:
            logger.critical("KILL SWITCH | every-client order view unavailable: no flatten (a foreign GTC "
                            "order could reverse it) | manual intervention required")
        still_working = None if all_orders is None else {con_id(t) for t in all_orders}

        flattenable_ids = {con_id(p) for p in flattenable}
        liquidation_trades = []
        for position in self._account_positions():
            if con_id(position) not in flattenable_ids or con_id(position) in manual_con_ids:
                continue
            qty = float(position.position)
            if self.liquidation_qty_limit is not None and abs(qty) > self.liquidation_qty_limit:
                logger.critical("KILL SWITCH FLATTEN skipped | position exceeds liquidation limit | "
                                "manual intervention required")
                continue
            # Unknown view, an order with an unknown contract (conId 0), or a live order on this
            # position: never flatten (fail closed).
            if still_working is None or 0 in still_working or con_id(position) in still_working:
                logger.critical("KILL SWITCH FLATTEN skipped | a working order on this position is still live "
                                "(other client or unconfirmed cancel) | manual intervention required")
                continue
            action = "SELL" if qty > 0 else "BUY"
            order = MarketOrder(action, abs(qty), tif="DAY")
            order.account = self.account
            try:
                self.guard.assert_can_transmit(order)
            except PaperGuardError as exc:
                logger.critical("KILL SWITCH FLATTEN refused by paper guard | %s", exc)
                continue
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
        # Flat means no position AND no working order from ANY client (a leftover GTC child could
        # reopen a position later).
        leftover_orders = await self._all_open_trades()
        flat = bool(final_state.is_flat) and leftover_orders is not None and not leftover_orders
        if not flat:
            logger.critical("KILL SWITCH INCOMPLETE | manual intervention required")
        else:
            logger.critical("KILL SWITCH COMPLETE | account=%s broker confirmed flat", mask_account(self.account))

        self._last_result = KillSwitchResult(
            triggered=True,
            dry_run=False,
            cancelled_orders=cancelled,
            liquidation_orders=len(liquidation_trades),
            flat_confirmed=flat,
            reason=reason,
        )
        return self._last_result
