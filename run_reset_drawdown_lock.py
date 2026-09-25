"""Human-only reset of the multi-day drawdown lock (no orders; local state only).

Reads the current NetLiquidation (read-only session), shows the current high-water mark,
drawdown and lock reason, and makes the current equity the new high-water mark. Requires the
literal ACK in the shell environment (an ACK persisted in ``.env`` is refused) and a session
verified as PAPER for the configured account. Run only after reviewing why the lock fired.
"""
from __future__ import annotations

import asyncio
from os import getenv

from config.config import persisted_env

from config.config import STATE_DIR, config, read_only_ibkr_settings
from core.account import AccountService
from core.connection import IBKRConnection
from core.paper_guard import mask_account, verify_paper_account
from risk.drawdown_guard import RESET_ACK, DrawdownStateStore


async def main(state_dir=None) -> None:
    if persisted_env().get("DRAWDOWN_RESET_ACK"):
        raise SystemExit("DRAWDOWN_RESET_ACK must not be stored in .env; export it for this run only")
    if getenv("DRAWDOWN_RESET_ACK", "") != RESET_ACK:
        raise SystemExit(f"Set DRAWDOWN_RESET_ACK={RESET_ACK} to reset the drawdown lock")
    if not config.ibkr.account:
        raise SystemExit("Set IBKR_ACCOUNT so the reset targets an explicit account")
    connection = IBKRConnection(read_only_ibkr_settings(config.ibkr, 51))
    try:
        ib = await connection.connect()
        # Verify the account type with the trading settings (readonly does not matter here).
        verification = verify_paper_account(ib, config.ibkr, config.ibkr.account)
        if not verification.verified:
            raise SystemExit(f"Refusing: session not verified as PAPER ({verification.reason})")
        account = await AccountService(ib).snapshot(config.ibkr.account, log=False)
        if not account.net_liquidation or account.net_liquidation <= 0:
            raise SystemExit("NetLiquidation unavailable; nothing reset")
        store = DrawdownStateStore((state_dir or STATE_DIR) / "drawdown_state.json")
        before = store.get(account.account)
        if before is not None:
            dd = max(0.0, (before.peak_equity - account.net_liquidation) / before.peak_equity) * 100
            print(f"CURRENT | locked={before.locked} drawdown={dd:.2f}% reason={before.lock_reason or '-'} "
                  f"peak_set_at={before.peak_at_utc}")
        record = store.reset(account.account, account.net_liquidation, RESET_ACK)
        print(f"DRAWDOWN RESET | account={mask_account(account.account)} "
              f"new_high_water_mark_set_at={record.peak_at_utc}")
    finally:
        await connection.disconnect()


if __name__ == "__main__":
    import argparse
    from pathlib import Path

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--state-dir", default=None,
                    help="state directory to reset (default: repo state/; shadow-only uses state/shadow_only)")
    args = ap.parse_args()
    asyncio.run(main(Path(args.state_dir) if args.state_dir else None))
