"""Supervised PAPER drill of the armed kill switch (human-run only; see docs/KILL_SWITCH_DRILL.md).

Default is DRY-RUN: it reports what the kill switch would cancel/flatten and sends nothing.
The armed drill additionally requires ``--armed`` AND ``KILL_SWITCH_DRILL_ACK`` set to the
literal below, a session verified as PAPER, and that every open position is a stock of at most
``--max-qty`` shares (so the drill can only flatten a tiny position the human opened for it).
All broker actions go through ``risk.kill_switch.KillSwitch`` and ``core.paper_guard``.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from os import getenv

from config.config import IBKRConfig, RiskConfig
from core.paper_guard import build_paper_guard, mask_account
from risk.kill_switch import KillSwitch

DRILL_ACK = "I_AM_WATCHING_AND_ACCEPT_THE_PAPER_KILL_SWITCH_DRILL"


@dataclass(frozen=True)
class DrillOutcome:
    ran: bool
    armed: bool
    reason: str
    cancelled: int = 0
    liquidation_orders: int = 0
    flat_confirmed: bool = False


async def run_drill(ib, settings: IBKRConfig, *, armed: bool, ack: str, max_qty: float = 1.0) -> DrillOutcome:
    guard = build_paper_guard(ib, settings, settings.account)
    if not guard.verification.verified:
        return DrillOutcome(False, armed, f"paper_unverified:{guard.verification.reason}")
    account = guard.account
    positions = [p for p in ib.positions() if p.account == account and float(p.position) != 0]
    oversized = [p for p in positions
                 if str(getattr(p.contract, "secType", "")).upper() != "STK" or abs(float(p.position)) > max_qty]
    if oversized:
        return DrillOutcome(False, armed, "positions_exceed_drill_limits")
    if armed and ack != DRILL_ACK:
        return DrillOutcome(False, armed, "missing_drill_ack")
    risk = RiskConfig(kill_switch_enabled=True, kill_switch_dry_run=not armed)
    result = await KillSwitch(ib, risk, account, guard=guard).execute("supervised paper drill")
    return DrillOutcome(True, armed, "executed", result.cancelled_orders, result.liquidation_orders,
                        result.flat_confirmed)


async def main_async(args) -> None:
    from config.config import config
    from core.connection import IBKRConnection

    connection = IBKRConnection(config.ibkr)
    try:
        ib = await connection.connect()
        outcome = await run_drill(ib, config.ibkr, armed=args.armed, ack=getenv("KILL_SWITCH_DRILL_ACK", ""),
                                  max_qty=args.max_qty)
        print(f"KILL SWITCH DRILL | account={mask_account(config.ibkr.account)} {outcome}")
    finally:
        await connection.disconnect()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--armed", action="store_true", help="actually cancel/flatten (paper only, needs ACK)")
    ap.add_argument("--max-qty", type=float, default=1.0)
    asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    main()
