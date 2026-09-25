from __future__ import annotations

from dataclasses import dataclass

from ib_async import IB

from core.paper_guard import mask_account
from utils.logger import logger


@dataclass(frozen=True)
class AccountSnapshot:
    account: str
    currency: str | None
    net_liquidation: float | None
    total_cash_value: float | None
    available_funds: float | None
    buying_power: float | None
    positions: int
    open_orders: int


class AccountService:
    """Read-only account/account-state helpers."""

    def __init__(self, ib: IB) -> None:
        self.ib = ib

    async def snapshot(
        self, configured_account: str | None = None, *, log: bool = True
    ) -> AccountSnapshot:
        if not self.ib.isConnected():
            raise RuntimeError("IBKR must be connected before reading account state")

        accounts = self.ib.managedAccounts()
        if not accounts:
            raise RuntimeError("IBKR returned no managed accounts")

        account = configured_account or accounts[0]
        if account not in accounts:
            raise RuntimeError(
                f"Configured account {mask_account(account)} is not available. "
                f"Managed accounts: {[mask_account(a) for a in accounts]}"
            )

        # accountSummaryAsync requests/refreshes the account summary and leaves
        # the latest values cached in IB. This is read-only.
        values = await self.ib.accountSummaryAsync(account)

        def number(tag: str) -> float | None:
            candidates = [v for v in values if v.account == account and v.tag == tag]
            if not candidates:
                return None
            # Prefer the aggregate/base-currency value when IBKR supplies one.
            preferred = next(
                (v for v in candidates if v.currency in {"BASE", ""}),
                candidates[0],
            )
            try:
                return float(preferred.value)
            except (TypeError, ValueError):
                return None

        currency_value = next(
            (v.currency for v in values if v.account == account and v.tag == "NetLiquidation"),
            None,
        )
        currency = None if currency_value in {None, "", "BASE"} else currency_value

        positions = [p for p in self.ib.positions() if p.account == account]
        open_trades = [
            trade
            for trade in self.ib.openTrades()
            if not getattr(trade.order, "account", "")
            or trade.order.account == account
        ]

        snapshot = AccountSnapshot(
            account=account,
            currency=currency,
            net_liquidation=number("NetLiquidation"),
            total_cash_value=number("TotalCashValue"),
            available_funds=number("AvailableFunds"),
            buying_power=number("BuyingPower"),
            positions=len(positions),
            open_orders=len(open_trades),
        )
        if log:
            self.log_snapshot(snapshot)
        return snapshot

    @staticmethod
    def _money(value: float | None, currency: str | None) -> str:
        if value is None:
            return "N/A"
        suffix = f" {currency}" if currency else ""
        return f"{value:,.2f}{suffix}"

    def log_snapshot(self, snapshot: AccountSnapshot) -> None:
        # Balances are private: logs record only whether each value was available.
        def present(value) -> str:
            return "available" if value is not None else "N/A"

        logger.info("ACCOUNT SNAPSHOT | account=%s", mask_account(snapshot.account))
        logger.info(
            "ACCOUNT SNAPSHOT | net_liquidation=%s total_cash=%s available_funds=%s buying_power=%s",
            present(snapshot.net_liquidation), present(snapshot.total_cash_value),
            present(snapshot.available_funds), present(snapshot.buying_power),
        )
        logger.info(
            "ACCOUNT SNAPSHOT | positions=%s open_orders=%s",
            snapshot.positions,
            snapshot.open_orders,
        )
