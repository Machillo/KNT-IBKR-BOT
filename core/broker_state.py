from __future__ import annotations

import asyncio
from dataclasses import dataclass

from ib_async import IB

from utils.logger import logger


@dataclass(frozen=True)
class BrokerState:
    account: str
    position_count: int
    open_order_count: int
    nonzero_positions: tuple[tuple[str, float], ...]

    @property
    def is_flat(self) -> bool:
        return self.position_count == 0 and self.open_order_count == 0


class BrokerStateService:
    """Reconciles the bot's assumptions with broker-reported state."""

    def __init__(self, ib: IB) -> None:
        self.ib = ib

    def snapshot(self, account: str) -> BrokerState:
        positions = [p for p in self.ib.positions() if p.account == account and float(p.position) != 0]

        open_trades = []
        for trade in self.ib.openTrades():
            if trade.isDone():
                continue
            order_account = (getattr(trade.order, "account", "") or "").strip()
            if order_account and order_account != account:
                continue
            open_trades.append(trade)

        normalized = tuple(
            (
                p.contract.localSymbol or p.contract.symbol or str(p.contract.conId),
                float(p.position),
            )
            for p in positions
        )
        return BrokerState(
            account=account,
            position_count=len(positions),
            open_order_count=len(open_trades),
            nonzero_positions=normalized,
        )

    async def wait_until_flat(self, account: str, timeout: float = 20.0) -> BrokerState:
        deadline = asyncio.get_running_loop().time() + timeout
        last = self.snapshot(account)
        while not last.is_flat and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.25)
            last = self.snapshot(account)

        if last.is_flat:
            logger.info("BROKER RECONCILIATION | account=%s state=FLAT open_orders=0", account)
        else:
            logger.error(
                "BROKER RECONCILIATION | account=%s state=NOT_FLAT positions=%s open_orders=%s details=%s",
                account,
                last.position_count,
                last.open_order_count,
                last.nonzero_positions,
            )
        return last
