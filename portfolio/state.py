from __future__ import annotations

from dataclasses import dataclass

from core.account import AccountSnapshot
from portfolio.brain import PortfolioSnapshot


@dataclass(frozen=True)
class PositionExposure:
    symbol: str
    asset_class: str
    quantity: float
    market_price: float
    notional: float
    con_id: int = 0
    exchange: str = ""
    currency: str = ""


@dataclass(frozen=True)
class PendingOrderExposure:
    symbol: str
    asset_class: str
    quantity: float
    reference_price: float
    notional: float


@dataclass(frozen=True)
class PortfolioState:
    snapshot: PortfolioSnapshot
    positions: tuple[PositionExposure, ...]
    pending_orders: tuple[PendingOrderExposure, ...]


class PortfolioStateService:
    """Read-only broker reconciliation for portfolio-level allocation decisions."""

    def __init__(self, ib) -> None:
        self.ib = ib

    @staticmethod
    def _contract_symbol(contract) -> str:
        return str(
            getattr(contract, "localSymbol", "")
            or getattr(contract, "symbol", "")
            or getattr(contract, "conId", "")
        )

    @staticmethod
    def _asset_class(contract) -> str:
        return str(getattr(contract, "secType", "") or "UNKNOWN").upper()

    def _position_exposures(self, account: str) -> tuple[PositionExposure, ...]:
        results: list[PositionExposure] = []
        for item in self.ib.portfolio(account):
            quantity = float(getattr(item, "position", 0.0) or 0.0)
            if quantity == 0:
                continue
            market_price = float(getattr(item, "marketPrice", 0.0) or 0.0)
            market_value = float(getattr(item, "marketValue", 0.0) or 0.0)
            notional = abs(market_value) if market_value else abs(quantity * market_price)
            contract = item.contract
            results.append(PositionExposure(
                symbol=self._contract_symbol(contract),
                asset_class=self._asset_class(contract),
                quantity=quantity,
                market_price=market_price,
                notional=max(0.0, notional),
                con_id=int(getattr(contract, "conId", 0) or 0),
                exchange=str(getattr(contract, "exchange", "") or getattr(contract, "primaryExchange", "") or ""),
                currency=str(getattr(contract, "currency", "") or ""),
            ))
        return tuple(results)

    def _pending_order_exposures(self, account: str) -> tuple[PendingOrderExposure, ...]:
        results: list[PendingOrderExposure] = []
        for trade in self.ib.openTrades():
            if trade.isDone():
                continue
            order_account = str(getattr(trade.order, "account", "") or "").strip()
            if order_account and order_account != account:
                continue

            total_qty = abs(float(getattr(trade.order, "totalQuantity", 0.0) or 0.0))
            filled_qty = abs(float(getattr(trade.orderStatus, "filled", 0.0) or 0.0))
            remaining_qty = max(0.0, total_qty - filled_qty)
            if remaining_qty <= 0:
                continue

            limit_price = float(getattr(trade.order, "lmtPrice", 0.0) or 0.0)
            aux_price = float(getattr(trade.order, "auxPrice", 0.0) or 0.0)
            reference_price = limit_price if limit_price > 0 else aux_price
            if reference_price <= 0:
                ticker = self.ib.ticker(trade.contract)
                reference_price = float(getattr(ticker, "marketPrice", lambda: 0.0)() or 0.0)
            notional = remaining_qty * max(0.0, reference_price)
            results.append(PendingOrderExposure(
                symbol=self._contract_symbol(trade.contract),
                asset_class=self._asset_class(trade.contract),
                quantity=remaining_qty,
                reference_price=max(0.0, reference_price),
                notional=max(0.0, notional),
            ))
        return tuple(results)

    def build(
        self,
        account: AccountSnapshot,
        *,
        starting_equity: float,
        daily_loss_limit_pct: float,
        trading_locked: bool,
    ) -> PortfolioState:
        if account.net_liquidation is None or account.net_liquidation <= 0:
            raise RuntimeError("Portfolio snapshot requires valid NetLiquidation")
        if starting_equity <= 0:
            raise ValueError("starting_equity must be > 0")

        positions = self._position_exposures(account.account)
        pending_orders = self._pending_order_exposures(account.account)
        current_equity = float(account.net_liquidation)
        daily_loss_used = max(0.0, starting_equity - current_equity)
        daily_loss_limit = starting_equity * max(0.0, float(daily_loss_limit_pct))

        snapshot = PortfolioSnapshot(
            net_liquidation=current_equity,
            cash=max(0.0, float(account.total_cash_value or 0.0)),
            committed_notional=sum(item.notional for item in positions),
            open_position_risk=0.0,
            pending_order_notional=sum(item.notional for item in pending_orders),
            daily_loss_used=daily_loss_used,
            daily_loss_limit=daily_loss_limit,
            trading_locked=bool(trading_locked),
        )
        return PortfolioState(snapshot=snapshot, positions=positions, pending_orders=pending_orders)
