"""Human-only reset of the multi-day drawdown lock (no orders; local state only).

Reads the current NetLiquidation (read-only) and makes it the new high-water mark.
Requires the literal ACK in DRAWDOWN_RESET_ACK. Run only after reviewing why the
lock fired.
"""
from __future__ import annotations

import asyncio
from os import getenv

from config.config import config, read_only_ibkr_settings
from core.account import AccountService
from core.connection import IBKRConnection
from core.paper_guard import mask_account
from risk.drawdown_guard import RESET_ACK, DrawdownStateStore


async def main() -> None:
    if getenv("DRAWDOWN_RESET_ACK", "") != RESET_ACK:
        raise SystemExit(f"Set DRAWDOWN_RESET_ACK={RESET_ACK} to reset the drawdown lock")
    connection = IBKRConnection(read_only_ibkr_settings(config.ibkr, 51))
    try:
        ib = await connection.connect()
        account = await AccountService(ib).snapshot(config.ibkr.account, log=False)
        if not account.net_liquidation or account.net_liquidation <= 0:
            raise SystemExit("NetLiquidation unavailable; nothing reset")
        store = DrawdownStateStore()
        before = store.get(account.account)
        record = store.reset(account.account, account.net_liquidation, RESET_ACK)
        print(f"DRAWDOWN RESET | account={mask_account(account.account)} was_locked={bool(before and before.locked)} "
              f"new_high_water_mark_set_at={record.peak_at_utc}")
    finally:
        await connection.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
