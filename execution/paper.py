from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
import sqlite3

from core.order_manager import OrderManager


@dataclass(frozen=True)
class PaperExecutionRequest:
    symbol: str
    strategy: str
    side: str
    quantity: float
    entry_price: float
    stop_price: float
    target_price: float
    regime: str


@dataclass(frozen=True)
class PaperExecutionResult:
    submitted: bool
    reason: str
    parent_order_id: int | None = None


class TradeJournalStore:
    """Append-only paper execution journal."""

    def __init__(self, path: str | Path = "state/strategy_performance.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS paper_trade_journal (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    entry_price REAL NOT NULL,
                    stop_price REAL NOT NULL,
                    target_price REAL NOT NULL,
                    regime TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    parent_order_id INTEGER,
                    source TEXT NOT NULL DEFAULT 'AUTONOMOUS_PAPER'
                )
                """
            )

    def record(
        self,
        request: PaperExecutionRequest,
        *,
        status: str,
        reason: str,
        parent_order_id: int | None = None,
    ) -> int:
        with sqlite3.connect(self.path) as conn:
            cur = conn.execute(
                """
                INSERT INTO paper_trade_journal (
                    created_at, symbol, strategy, side, quantity, entry_price, stop_price,
                    target_price, regime, status, reason, parent_order_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now(timezone.utc).isoformat(), request.symbol.upper(), request.strategy,
                    request.side.upper(), float(request.quantity), float(request.entry_price),
                    float(request.stop_price), float(request.target_price), request.regime,
                    status.upper(), reason, parent_order_id,
                ),
            )
            return int(cur.lastrowid)


class PaperExecutionEngine:
    """Fail-closed execution gate for autonomous Paper Trading only."""

    def __init__(
        self,
        ib,
        *,
        account: str,
        enabled: bool = False,
        paper_authorized: bool = False,
        risk_manager=None,
        journal: TradeJournalStore | None = None,
        acceptance_delay_seconds: float = 0.35,
        session_policy=None,
    ) -> None:
        self.ib = ib
        self.account = account
        self.enabled = bool(enabled)
        self.paper_authorized = bool(paper_authorized)
        self.risk_manager = risk_manager
        self.orders = OrderManager(ib, account=account)
        self.journal = journal or TradeJournalStore()
        self.acceptance_delay_seconds = max(0.0, float(acceptance_delay_seconds))
        self.session_policy = session_policy

    @staticmethod
    def _normalize_stock_price(value: float) -> float:
        return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

    def _normalize_request(self, contract, request: PaperExecutionRequest) -> PaperExecutionRequest | None:
        sec_type = str(getattr(contract, "secType", "") or "").upper()
        if sec_type not in {"STK", ""}:
            return None
        return replace(
            request,
            entry_price=self._normalize_stock_price(request.entry_price),
            stop_price=self._normalize_stock_price(request.stop_price),
            target_price=self._normalize_stock_price(request.target_price),
        )

    @staticmethod
    def _valid_geometry(request: PaperExecutionRequest) -> bool:
        side = request.side.upper()
        if side == "LONG":
            return request.stop_price < request.entry_price < request.target_price
        if side == "SHORT":
            return request.target_price < request.entry_price < request.stop_price
        return False

    def _session_allows(self, contract) -> bool:
        if self.session_policy is None:
            return True
        sec_type = str(getattr(contract, "secType", "") or "").upper()
        if sec_type not in {"STK", ""}:
            return False
        try:
            return bool(self.session_policy.state().market_open)
        except Exception:
            return False

    def _has_duplicate(self, symbol: str) -> bool:
        target = symbol.upper()
        for item in self.ib.portfolio(self.account):
            contract = getattr(item, "contract", None)
            existing = str(getattr(contract, "localSymbol", "") or getattr(contract, "symbol", "")).upper()
            quantity = float(getattr(item, "position", 0.0) or 0.0)
            if existing == target and quantity != 0:
                return True
        for trade in self.ib.openTrades():
            if trade.isDone():
                continue
            contract = getattr(trade, "contract", None)
            existing = str(getattr(contract, "localSymbol", "") or getattr(contract, "symbol", "")).upper()
            order_account = str(getattr(trade.order, "account", "") or "")
            if existing == target and (not order_account or order_account == self.account):
                return True
        return False

    @staticmethod
    def _trade_error(trade) -> tuple[bool, str | None]:
        status = str(getattr(getattr(trade, "orderStatus", None), "status", "") or "")
        if status in {"Cancelled", "ApiCancelled", "Inactive"}:
            return True, f"order_status_{status.lower()}"
        for entry in list(getattr(trade, "log", ()) or ()):
            error_code = int(getattr(entry, "errorCode", 0) or 0)
            if error_code:
                return True, f"broker_error_{error_code}"
        return False, None

    def _cancel_remaining(self, trades) -> None:
        for trade in reversed(tuple(trades)):
            try:
                if not trade.isDone():
                    self.orders.cancel(trade)
            except Exception:
                pass

    def _flatten_parent_fill(self, contract, parent_trade) -> float:
        filled = float(getattr(parent_trade.orderStatus, "filled", 0.0) or 0.0)
        if filled <= 0:
            return 0.0
        parent_action = str(getattr(parent_trade.order, "action", "") or "").upper()
        flatten_action = "SELL" if parent_action == "BUY" else "BUY"
        self.orders.market(contract, flatten_action, filled)
        return filled

    async def submit(self, contract, request: PaperExecutionRequest) -> PaperExecutionResult:
        if not self.enabled:
            self.journal.record(request, status="BLOCKED", reason="autonomous_paper_disabled")
            return PaperExecutionResult(False, "autonomous_paper_disabled")
        if not self.paper_authorized:
            self.journal.record(request, status="BLOCKED", reason="paper_execution_not_authorized")
            return PaperExecutionResult(False, "paper_execution_not_authorized")
        if not self._session_allows(contract):
            self.journal.record(request, status="BLOCKED", reason="market_session_closed")
            return PaperExecutionResult(False, "market_session_closed")
        if self.risk_manager is not None and bool(getattr(self.risk_manager, "trading_locked", False)):
            self.journal.record(request, status="BLOCKED", reason="risk_manager_locked")
            return PaperExecutionResult(False, "risk_manager_locked")
        if request.quantity <= 0 or request.entry_price <= 0 or request.stop_price <= 0 or request.target_price <= 0:
            self.journal.record(request, status="REJECTED", reason="invalid_execution_request")
            return PaperExecutionResult(False, "invalid_execution_request")
        if request.side.upper() not in {"LONG", "SHORT"}:
            self.journal.record(request, status="REJECTED", reason="unsupported_side")
            return PaperExecutionResult(False, "unsupported_side")

        normalized = self._normalize_request(contract, request)
        if normalized is None:
            self.journal.record(request, status="REJECTED", reason="unsupported_tick_normalization")
            return PaperExecutionResult(False, "unsupported_tick_normalization")
        if not self._valid_geometry(normalized):
            self.journal.record(normalized, status="REJECTED", reason="invalid_price_geometry")
            return PaperExecutionResult(False, "invalid_price_geometry")
        if self._has_duplicate(normalized.symbol):
            self.journal.record(normalized, status="REJECTED", reason="duplicate_symbol_exposure")
            return PaperExecutionResult(False, "duplicate_symbol_exposure")

        action = "BUY" if normalized.side.upper() == "LONG" else "SELL"
        trades = self.orders.bracket_limit(
            contract,
            action,
            normalized.quantity,
            entry_price=normalized.entry_price,
            take_profit_price=normalized.target_price,
            stop_price=normalized.stop_price,
            adaptive_parent=True,
        )
        parent_id = int(trades[0].order.orderId)
        self.journal.record(normalized, status="PENDING", reason="bracket_sent", parent_order_id=parent_id)

        if self.acceptance_delay_seconds:
            await asyncio.sleep(self.acceptance_delay_seconds)

        failures = [self._trade_error(trade) for trade in trades]
        rejected = [reason for failed, reason in failures if failed]
        if rejected:
            self._cancel_remaining(trades)
            flattened = self._flatten_parent_fill(contract, trades[0])
            reason = rejected[0] or "bracket_leg_rejected"
            if flattened > 0:
                reason = f"{reason}_flatten_requested"
            self.journal.record(normalized, status="FAILED", reason=reason, parent_order_id=parent_id)
            return PaperExecutionResult(False, reason, parent_id)

        accepted_statuses = {"PendingSubmit", "PreSubmitted", "Submitted", "Filled"}
        statuses = {
            str(getattr(getattr(trade, "orderStatus", None), "status", "") or "")
            for trade in trades
        }
        if not statuses or any(status not in accepted_statuses for status in statuses):
            self._cancel_remaining(trades)
            flattened = self._flatten_parent_fill(contract, trades[0])
            reason = "bracket_acceptance_unconfirmed"
            if flattened > 0:
                reason += "_flatten_requested"
            self.journal.record(normalized, status="FAILED", reason=reason, parent_order_id=parent_id)
            return PaperExecutionResult(False, reason, parent_id)

        self.journal.record(normalized, status="SUBMITTED", reason="bracket_confirmed", parent_order_id=parent_id)
        return PaperExecutionResult(True, "bracket_confirmed", parent_id)
