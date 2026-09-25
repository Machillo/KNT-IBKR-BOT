"""Fail-closed verification that the connected IBKR session is a PAPER account.

The IBKR API exposes no explicit "paper" flag. KNT therefore requires several
independent observations to agree before any order may be transmitted:

1. configuration: ALLOW_LIVE_TRADING=false and IBKR_PORT is a paper port;
2. the socket actually in use (``ib.client.port``) equals the configured port;
3. the session is connected;
4. EVERY account returned by ``managedAccounts()`` uses an IBKR paper prefix
   (``DU`` individual paper, ``DF`` advisor paper). A session that lists any
   other account is treated as live;
5. the target account is one of those managed accounts, and matches
   IBKR_ACCOUNT when that is configured; with several paper accounts and no
   IBKR_ACCOUNT the target is ambiguous and refused.

Ports are configurable in TWS/Gateway, so the port alone proves nothing; the
account-prefix check is the strongest signal the API offers. Anything that
cannot be verified is refused: no verification -> no order.

Verification is re-run immediately before every transmission (``PaperOrderGuard``)
so a reconnect to a different session cannot reuse a stale approval.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from config.config import IBKRConfig

PAPER_ACCOUNT_RE = re.compile(r"^D[UF]\d{4,12}$")


class PaperGuardError(RuntimeError):
    """Raised when an order transmission is refused by the paper guard."""


def mask_account(account: str | None) -> str:
    """Log-safe account label: keeps the prefix class and last 2 characters."""
    if not account:
        return "<none>"
    text = str(account)
    prefix = text[:2] if text[:2].isalpha() else text[:1]
    return f"{prefix}***{text[-2:]}" if len(text) > 4 else "***"


_ANY_ACCOUNT_RE = re.compile(r"\b(?:DU|DF|DI|U|F|I)\d{5,12}\b")


def redact_accounts(text: str | None) -> str:
    """Mask anything that looks like an IBKR account code inside free text."""
    if not text:
        return ""
    return _ANY_ACCOUNT_RE.sub(lambda m: mask_account(m.group(0)), str(text))


def is_paper_account_id(account: str | None) -> bool:
    return bool(account) and bool(PAPER_ACCOUNT_RE.fullmatch(str(account).strip()))


@dataclass(frozen=True)
class PaperVerification:
    verified: bool
    account: str
    reason: str
    configured_port: int
    connected_port: int | None

    @property
    def masked_account(self) -> str:
        return mask_account(self.account)


def _connected_port(ib) -> int | None:
    client = getattr(ib, "client", None)
    port = getattr(client, "port", None)
    try:
        port = int(port)
    except (TypeError, ValueError):
        return None
    return port if port > 0 else None


def verify_paper_account(ib, settings: IBKRConfig, account: str | None = None) -> PaperVerification:
    connected_port = _connected_port(ib)

    def refuse(reason: str, target: str = "") -> PaperVerification:
        return PaperVerification(False, target, reason, settings.port, connected_port)

    if settings.allow_live_trading:
        return refuse("live_trading_allowed_in_config")
    if settings.port not in settings.paper_ports or settings.port in settings.live_ports:
        return refuse("configured_port_not_paper")
    try:
        connected = bool(ib.isConnected())
    except Exception:
        connected = False
    if not connected:
        return refuse("not_connected")
    if connected_port is None:
        return refuse("connected_port_unknown")
    if connected_port != settings.port:
        return refuse("connected_port_mismatch")

    try:
        managed = [str(a).strip() for a in (ib.managedAccounts() or []) if str(a).strip()]
    except Exception:
        return refuse("managed_accounts_unavailable")
    if not managed:
        return refuse("no_managed_accounts")
    if not all(is_paper_account_id(a) for a in managed):
        return refuse("non_paper_account_in_session")

    configured = (settings.account or "").strip() or None
    requested = (account or "").strip() or None
    if configured and requested and configured != requested:
        return refuse("account_differs_from_configured", requested)
    target = requested or configured
    if target is None:
        if len(managed) != 1:
            return refuse("ambiguous_account_selection")
        target = managed[0]
    if target not in managed:
        return refuse("account_not_managed_by_session", target)
    if not is_paper_account_id(target):
        return refuse("account_not_paper", target)
    return PaperVerification(True, target, "verified_paper", settings.port, connected_port)


class PaperOrderGuard:
    """The only object allowed to authorize ``ib.placeOrder`` in KNT.

    It holds the startup verification and re-verifies the live session before
    every transmission. Orders must carry the verified account explicitly.
    """

    def __init__(self, ib, settings: IBKRConfig, verification: PaperVerification) -> None:
        self.ib = ib
        self.settings = settings
        self.verification = verification

    @property
    def account(self) -> str:
        return self.verification.account

    def assert_can_transmit(self, order) -> None:
        if not isinstance(self.verification, PaperVerification) or not self.verification.verified:
            raise PaperGuardError(
                f"Paper account not verified: {getattr(self.verification, 'reason', 'missing')}"
            )
        current = verify_paper_account(self.ib, self.settings, self.verification.account)
        if not current.verified or current.account != self.verification.account:
            raise PaperGuardError(f"Paper re-verification failed: {current.reason}")
        order_account = (getattr(order, "account", "") or "").strip()
        if order_account != self.verification.account:
            raise PaperGuardError("Order account missing or different from verified paper account")


def build_paper_guard(ib, settings: IBKRConfig, account: str | None = None) -> PaperOrderGuard:
    return PaperOrderGuard(ib, settings, verify_paper_account(ib, settings, account))
