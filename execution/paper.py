from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
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

    def record(self, request: PaperExecutionRequest, *, status: str, reason: str, parent_order_id: int | None = None) -> int:
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
    """Execution gate for autonomous Paper Trading only.

    Live ports remain blocked by configuration. This class submits only after the
    upstream strategy, learning, hard-risk and portfolio gates have approved a setup.
    """

    def __init__(self, ib, *, account: str, enabled: bool = False, journal: TradeJournalStore | None = None) -> None:
        self.ib = ib
        self.account = account
        self.enabled = bool(enabled)
        self.orders = OrderManager(ib, account=account)
        self.journal = journal or TradeJournalStore()

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

    def submit(self, contract, request: PaperExecutionRequest) -> PaperExecutionResult:
        if not self.enabled:
            self.journal.record(request, status="BLOCKED", reason="autonomous_paper_disabled")
            return PaperExecutionResult(False, "autonomous_paper_disabled")
        if request.quantity <= 0 or request.entry_price <= 0 or request.stop_price <= 0 or request.target_price <= 0:
            self.journal.record(request, status="REJECTED", reason="invalid_execution_request")
            return PaperExecutionResult(False, "invalid_execution_request")
        if request.side.upper() not in {"LONG", "SHORT"}:
            self.journal.record(request, status="REJECTED", reason="unsupported_side")
            return PaperExecutionResult(False, "unsupported_side")
        if self._has_duplicate(request.symbol):
            self.journal.record(request, status="REJECTED", reason="duplicate_symbol_exposure")
            return PaperExecutionResult(False, "duplicate_symbol_exposure")

        action = "BUY" if request.side.upper() == "LONG" else "SELL"
        trades = self.orders.bracket_limit(
            contract,
            action,
            request.quantity,
            entry_price=request.entry_price,
            take_profit_price=request.target_price,
            stop_price=request.stop_price,
            adaptive_parent=True,
        )
        parent_id = int(trades[0].order.orderId)
        self.journal.record(request, status="SUBMITTED", reason="bracket_submitted", parent_order_id=parent_id)
        return PaperExecutionResult(True, "bracket_submitted", parent_id)
