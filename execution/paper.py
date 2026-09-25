from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from os import getenv
from pathlib import Path
import sqlite3

from core.exceptions import RiskRejectedError
from core.order_manager import OrderManager
from core.paper_guard import PaperGuardError, PaperOrderGuard

# Literal acknowledgement required, in addition to AUTONOMOUS_TRADING_ENABLED=true,
# before the long-running loop may hand setups to the paper executor.
AUTONOMOUS_PAPER_ACK = "I_UNDERSTAND_KNT_WILL_SUBMIT_AUTONOMOUS_PAPER_ORDERS"

# Market-data types accepted as a fresh execution reference. 1 = live. Frozen (2)
# and delayed (3/4) quotes can be minutes old and are refused for entries.
FRESH_MARKET_DATA_TYPES = frozenset({1})


def autonomous_paper_armed(autonomous_trading_enabled: bool, ack: str | None = None) -> bool:
    """Both the config flag and the literal per-session ACK are required."""
    ack_value = getenv("AUTONOMOUS_PAPER_ACK", "") if ack is None else ack
    return bool(autonomous_trading_enabled) and ack_value == AUTONOMOUS_PAPER_ACK


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
    # Net liquidation used for the executor's own hard-risk re-check.
    account_equity: float = 0.0
    # Fresh quote taken immediately before submission and its IBKR data type.
    reference_price: float | None = None
    market_data_type: int | None = None
    con_id: int = 0


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

    def submitted_count_on(self, utc_date: str) -> int:
        """Entries sent to the broker on a UTC date (PENDING covers crashes mid-submit)."""
        with sqlite3.connect(self.path) as conn:
            row = conn.execute(
                """
                SELECT COUNT(DISTINCT COALESCE(parent_order_id, -id)) FROM paper_trade_journal
                WHERE substr(created_at, 1, 10)=? AND status IN ('PENDING', 'SUBMITTED', 'FAILED')
                  AND parent_order_id IS NOT NULL
                """,
                (utc_date,),
            ).fetchone()
        return int(row[0] or 0)


class PaperExecutionEngine:
    """Fail-closed execution gate for autonomous Paper Trading only.

    Every check below must pass, in order, before a bracket is transmitted. The
    engine re-applies the hard risk check itself, so no caller (strategy, runner
    or future module) can bypass the risk pipeline by calling ``submit`` directly.
    """

    def __init__(
        self,
        ib,
        *,
        account: str,
        enabled: bool = False,
        paper_guard: PaperOrderGuard | None = None,
        risk_manager=None,
        journal: TradeJournalStore | None = None,
        acceptance_delay_seconds: float = 0.35,
        session_policy=None,
        max_entries_per_day: int = 3,
        max_reference_deviation_pct: float = 0.015,
        allow_short: bool = False,
    ) -> None:
        self.ib = ib
        self.account = account
        self.enabled = bool(enabled)
        self.paper_guard = paper_guard
        self.risk_manager = risk_manager
        self.orders = OrderManager(ib, account=account, guard=paper_guard) if (
            paper_guard is not None and paper_guard.account == account
        ) else OrderManager(ib, account=account, guard=None)
        self.journal = journal or TradeJournalStore()
        self.acceptance_delay_seconds = max(0.0, float(acceptance_delay_seconds))
        self.session_policy = session_policy
        self.max_entries_per_day = max(0, int(max_entries_per_day))
        self.max_reference_deviation_pct = max(0.0, float(max_reference_deviation_pct))
        # Shorts stay disabled until shortability/borrow/SSR checks exist.
        self.allow_short = bool(allow_short)
        self._in_flight: set[str] = set()

    @staticmethod
    def _normalize_stock_price(value: float) -> float:
        return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

    def _normalize_request(self, contract, request: PaperExecutionRequest) -> PaperExecutionRequest | None:
        sec_type = str(getattr(contract, "secType", "") or "").upper()
        if sec_type != "STK":
            return None
        if float(request.quantity) != int(request.quantity):
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
        # Fail closed: without a session policy the market is treated as closed.
        if self.session_policy is None:
            return False
        sec_type = str(getattr(contract, "secType", "") or "").upper()
        if sec_type != "STK":
            return False
        try:
            return bool(self.session_policy.state().market_open)
        except Exception:
            return False

    def _guard_refusal(self) -> str | None:
        guard = self.paper_guard
        if guard is None:
            return "paper_guard_missing"
        if not guard.verification.verified:
            return "paper_account_unverified"
        if guard.account != self.account:
            return "paper_guard_account_mismatch"
        probe = type("_Probe", (), {"account": self.account})()
        try:
            guard.assert_can_transmit(probe)
        except PaperGuardError:
            return "paper_reverification_failed"
        return None

    def _connected(self) -> bool:
        try:
            return bool(self.ib.isConnected())
        except Exception:
            return False

    def _reference_refusal(self, request: PaperExecutionRequest) -> str | None:
        if request.market_data_type not in FRESH_MARKET_DATA_TYPES:
            return "reference_not_live_market_data"
        ref = request.reference_price
        if ref is None or ref <= 0:
            return "fresh_reference_price_missing"
        if abs(request.entry_price - ref) / ref > self.max_reference_deviation_pct:
            return "entry_far_from_fresh_reference"
        return None

    def _hard_risk_refusal(self, request: PaperExecutionRequest) -> str | None:
        if self.risk_manager is None:
            return "risk_manager_required"
        evaluate = getattr(self.risk_manager, "evaluate_trade", None)
        if evaluate is None:
            return "risk_manager_required"
        try:
            decision = evaluate(
                equity=float(request.account_equity),
                entry_price=float(request.entry_price),
                stop_price=float(request.stop_price),
                quantity=float(request.quantity),
            )
        except RiskRejectedError:
            return "hard_risk:invalid_inputs"
        if not getattr(decision, "approved", False):
            return f"hard_risk:{getattr(decision, 'reason', 'rejected')}"
        return None

    def _has_duplicate(self, symbol: str, con_id: int = 0) -> bool:
        target = symbol.upper()

        def same(contract) -> bool:
            existing = str(getattr(contract, "localSymbol", "") or getattr(contract, "symbol", "")).upper()
            existing_con = int(getattr(contract, "conId", 0) or 0)
            return existing == target or (con_id > 0 and existing_con == con_id)

        for item in self.ib.portfolio(self.account):
            quantity = float(getattr(item, "position", 0.0) or 0.0)
            if quantity != 0 and same(getattr(item, "contract", None)):
                return True
        for trade in self.ib.openTrades():
            if trade.isDone():
                continue
            order_account = str(getattr(trade.order, "account", "") or "")
            if same(getattr(trade, "contract", None)) and (not order_account or order_account == self.account):
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

    def _reject(self, request: PaperExecutionRequest, status: str, reason: str) -> PaperExecutionResult:
        self.journal.record(request, status=status, reason=reason)
        return PaperExecutionResult(False, reason)

    async def submit(self, contract, request: PaperExecutionRequest) -> PaperExecutionResult:
        if not self.enabled:
            return self._reject(request, "BLOCKED", "autonomous_paper_disabled")
        guard_reason = self._guard_refusal()
        if guard_reason is not None:
            return self._reject(request, "BLOCKED", guard_reason)
        if not self._connected():
            return self._reject(request, "BLOCKED", "broker_disconnected")
        if not self._session_allows(contract):
            return self._reject(request, "BLOCKED", "market_session_closed")
        if self.risk_manager is None:
            return self._reject(request, "BLOCKED", "risk_manager_required")
        if bool(getattr(self.risk_manager, "trading_locked", False)):
            return self._reject(request, "BLOCKED", "risk_manager_locked")
        if request.quantity <= 0 or request.entry_price <= 0 or request.stop_price <= 0 or request.target_price <= 0:
            return self._reject(request, "REJECTED", "invalid_execution_request")
        side = request.side.upper()
        if side not in {"LONG", "SHORT"}:
            return self._reject(request, "REJECTED", "unsupported_side")
        if side == "SHORT" and not self.allow_short:
            return self._reject(request, "REJECTED", "short_entries_disabled")
        today = datetime.now(timezone.utc).date().isoformat()
        if self.journal.submitted_count_on(today) >= self.max_entries_per_day:
            return self._reject(request, "BLOCKED", "daily_entry_limit_reached")

        normalized = self._normalize_request(contract, request)
        if normalized is None:
            return self._reject(request, "REJECTED", "unsupported_instrument_or_quantity")
        if not self._valid_geometry(normalized):
            return self._reject(normalized, "REJECTED", "invalid_price_geometry")
        reference_reason = self._reference_refusal(normalized)
        if reference_reason is not None:
            return self._reject(normalized, "REJECTED", reference_reason)
        risk_reason = self._hard_risk_refusal(normalized)
        if risk_reason is not None:
            return self._reject(normalized, "REJECTED", risk_reason)
        con_id = int(normalized.con_id or getattr(contract, "conId", 0) or 0)
        key = f"{normalized.symbol.upper()}:{con_id}"
        if key in self._in_flight or self._has_duplicate(normalized.symbol, con_id):
            return self._reject(normalized, "REJECTED", "duplicate_symbol_exposure")

        action = "BUY" if side == "LONG" else "SELL"
        # In-flight marker covers the window before the broker reports the new
        # orders; afterwards openTrades()/portfolio() carry the duplicate check.
        self._in_flight.add(key)
        try:
            return await self._transmit_bracket(contract, normalized, action)
        finally:
            self._in_flight.discard(key)

    async def _transmit_bracket(self, contract, normalized: PaperExecutionRequest,
                                action: str) -> PaperExecutionResult:
        try:
            trades = self.orders.bracket_limit(
                contract,
                action,
                normalized.quantity,
                entry_price=normalized.entry_price,
                take_profit_price=normalized.target_price,
                stop_price=normalized.stop_price,
                adaptive_parent=True,
            )
        except PaperGuardError:
            return self._reject(normalized, "BLOCKED", "paper_guard_refused")
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
