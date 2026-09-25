"""Human-only reset of the persistent execution lock (no orders; local state only).

The executor sets ``state/execution_lock.json`` whenever the broker state became uncertain after
a transmission. This command shows why, verifies with a READ-ONLY session that the paper
account is flat (no positions, no open orders from ANY client), and only then removes the
lock. Requires the literal ACK in the shell environment (refused if stored in ``.env``).
"""
from __future__ import annotations

import asyncio
from os import getenv


async def main() -> None:
    from config.config import STATE_DIR, config, persisted_env, read_only_ibkr_settings
    from core.connection import IBKRConnection
    from execution.preflight import broker_checks
    from risk.execution_lock import RESET_ACK, ExecutionLockStore

    if persisted_env().get("EXECUTION_LOCK_RESET_ACK"):
        raise SystemExit("EXECUTION_LOCK_RESET_ACK must not be stored in .env; export it for this run only")
    store = ExecutionLockStore(STATE_DIR / "execution_lock.json")
    record = store.read()
    if record is None:
        print("EXECUTION LOCK | none")
        return
    print(f"EXECUTION LOCK | reason={record.get('reason')} events={len(record.get('events', []))}")
    if getenv("EXECUTION_LOCK_RESET_ACK", "") != RESET_ACK:
        raise SystemExit(f"Check TWS first, then set EXECUTION_LOCK_RESET_ACK={RESET_ACK} to clear the lock")
    connection = IBKRConnection(read_only_ibkr_settings(config.ibkr, 56))
    try:
        ib = await connection.connect()
        checks, _ = await broker_checks(ib, config, state_dir=STATE_DIR)
    finally:
        await connection.disconnect()
    required = {"paper_account_verifiable", "account_flat", "no_open_orders_any_client"}
    failed = [c.name for c in checks if c.name in required and not c.ok]
    if failed or not required <= {c.name for c in checks}:
        raise SystemExit(f"Refusing: broker state not verified clean ({failed or 'checks incomplete'}); lock kept")
    store.clear(getenv("EXECUTION_LOCK_RESET_ACK", ""))
    print("EXECUTION LOCK | cleared")


if __name__ == "__main__":
    asyncio.run(main())
