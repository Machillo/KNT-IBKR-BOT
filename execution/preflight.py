"""Read-only preflight for a supervised PAPER plumbing test (docs/PAPER_PLUMBING_TEST.md).

Nothing here can place, modify or cancel an order; every broker call is a read request
(managed accounts, account summary, positions, all-client open orders, one market-data
snapshot, the trading calendar). Each check returns PASS/FAIL with a reason; the overall
result is READY only when every REQUIRED check passes. It never arms anything.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from core.paper_guard import verify_paper_account
from risk.persisted_locks import persisted_lock_reason

ACK_NAMES = ("KNT_SIGNAL_PAPER_ACK", "AUTONOMOUS_PAPER_ACK", "KILL_SWITCH_DRILL_ACK", "PAPER_SMOKE_ACK",
             "DRAWDOWN_RESET_ACK")
MAX_PLUMBING_QTY = 1.0


@dataclass(frozen=True)
class PreflightCheck:
    name: str
    ok: bool
    detail: str
    required: bool = True


def config_checks(cfg, env_file: dict | None) -> list[PreflightCheck]:
    ibkr, risk, runtime = cfg.ibkr, cfg.risk, cfg.runtime
    persisted = sorted(k for k in ACK_NAMES if (env_file or {}).get(k))
    return [
        PreflightCheck("paper_port", ibkr.port in ibkr.paper_ports and ibkr.port not in ibkr.live_ports,
                       f"port={ibkr.port}"),
        PreflightCheck("live_trading_disallowed", not ibkr.allow_live_trading, "ALLOW_LIVE_TRADING must be false"),
        PreflightCheck("kill_switch_enabled", bool(risk.kill_switch_enabled), "KILL_SWITCH_ENABLED must be true"),
        PreflightCheck("market_data_type_live", cfg.market_data.market_data_type == 1,
                       f"MARKET_DATA_TYPE={cfg.market_data.market_data_type} (entries need 1)"),
        PreflightCheck("require_flat_startup", bool(runtime.require_flat_startup), "REQUIRE_FLAT_STARTUP must be true"),
        PreflightCheck("autonomous_off", not runtime.autonomous_trading_enabled,
                       "AUTONOMOUS_TRADING_ENABLED must be false for a plumbing test"),
        PreflightCheck("no_ack_persisted", not persisted,
                       "ACKs must be given per run, never stored in .env" + (f": {persisted}" if persisted else "")),
    ]


async def broker_checks(ib, cfg, *, state_dir: Path, market_data=None, probe_contract=None,
                        session_policy=None) -> tuple[list[PreflightCheck], str | None]:
    checks: list[PreflightCheck] = []
    # Verification as if the session were a trading one; a readonly preflight session is expected.
    verification = verify_paper_account(ib, replace(cfg.ibkr, readonly=False), cfg.ibkr.account)
    checks.append(PreflightCheck("paper_account_verifiable", verification.verified, verification.reason))
    account = verification.account if verification.verified else None
    if account is None:
        return checks, None

    try:
        summary = await ib.accountSummaryAsync(account)
        net_liq = next((float(v.value) for v in summary
                        if getattr(v, "tag", "") == "NetLiquidation" and getattr(v, "account", account) == account), None)
    except Exception as exc:
        net_liq = None
        checks.append(PreflightCheck("net_liquidation", False, type(exc).__name__))
    else:
        checks.append(PreflightCheck("net_liquidation", net_liq is not None and net_liq > 0,
                                     "available" if net_liq else "missing"))

    positions = [p for p in (ib.positions() or []) if getattr(p, "account", account) == account
                 and float(getattr(p, "position", 0) or 0) != 0]
    checks.append(PreflightCheck("account_flat", not positions, f"positions={len(positions)}"))
    try:
        all_open = await ib.reqAllOpenOrdersAsync()
        working = [t for t in (all_open or []) if not t.isDone()
                   and getattr(getattr(t, "order", None), "account", account) in ("", account)]
        checks.append(PreflightCheck("no_open_orders_any_client", not working, f"open_orders={len(working)}"))
    except Exception as exc:
        checks.append(PreflightCheck("no_open_orders_any_client", False, f"unverifiable: {type(exc).__name__}"))

    if market_data is not None and probe_contract is not None:
        try:
            snap = await market_data.snapshot_contract(probe_contract, getattr(probe_contract, "symbol", None),
                                                       timeout=5.0)
            live = getattr(snap, "market_data_type", None) == 1
            two_sided = bool(snap.bid and snap.ask and 0 < snap.bid <= snap.ask)
            checks.append(PreflightCheck("live_two_sided_quote", live and two_sided,
                                         f"type={getattr(snap, 'market_data_type', None)} two_sided={two_sided}"))
        except Exception as exc:
            checks.append(PreflightCheck("live_two_sided_quote", False, type(exc).__name__))
    else:
        checks.append(PreflightCheck("live_two_sided_quote", False, "not checked"))

    if session_policy is not None:
        try:
            refresh = getattr(session_policy, "refresh", None)
            if refresh is not None:
                await refresh(ib)
            state = session_policy.state()
            checks.append(PreflightCheck("regular_session_open", bool(state.market_open),
                                         f"session={state.session}", required=False))
        except Exception as exc:
            checks.append(PreflightCheck("regular_session_open", False, type(exc).__name__, required=False))

    trading_date = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    lock = persisted_lock_reason(state_dir, account, trading_date)
    checks.append(PreflightCheck("no_persisted_lock", lock is None, lock or "none"))
    return checks, account


def verdict(checks: list[PreflightCheck]) -> tuple[str, list[str]]:
    failed = [c.name for c in checks if c.required and not c.ok]
    return ("READY_FOR_SUPERVISED_PLUMBING" if not failed else "NOT_READY"), failed
