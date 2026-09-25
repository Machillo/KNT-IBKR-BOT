"""Read-only PAPER preflight (docs/PAPER_PLUMBING_TEST.md). Sends no orders, arms nothing.

Connects with a read-only session and its own clientId, then checks configuration, the paper
account, flatness (positions AND every client's open orders), a live two-sided quote, the
trading session and the bot's persisted locks. Prints READY_FOR_SUPERVISED_PLUMBING or
NOT_READY with the failing checks. The account id is never printed.
"""
from __future__ import annotations

import argparse
import asyncio


async def main_async(args) -> int:
    from ib_async import Stock

    from config.config import STATE_DIR, config, persisted_env, read_only_ibkr_settings
    from core.connection import IBKRConnection
    from core.market_data import MarketDataService
    from execution.preflight import broker_checks, config_checks, verdict
    from market.session import BrokerCalendarSessionPolicy

    checks = config_checks(config, persisted_env())
    settings = read_only_ibkr_settings(config.ibkr, 55)
    connection = IBKRConnection(settings)
    try:
        ib = await connection.connect()
        market_data = MarketDataService(ib, config.market_data)
        market_data.configure()
        probe = Stock(args.probe_symbol, "SMART", "USD")
        more, _ = await broker_checks(ib, config, state_dir=STATE_DIR, market_data=market_data,
                                      probe_contract=probe, session_policy=BrokerCalendarSessionPolicy())
        checks += more
    finally:
        await connection.disconnect()
    for c in checks:
        print(f"{'PASS' if c.ok else ('FAIL' if c.required else 'WARN')} | {c.name} | {c.detail}")
    result, failed = verdict(checks)
    print(f"PREFLIGHT | {result}" + (f" | failed={failed}" if failed else ""))
    return 0 if not failed else 1


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--probe-symbol", default="SPY", help="liquid symbol used only to test live quotes")
    raise SystemExit(asyncio.run(main_async(ap.parse_args())))


if __name__ == "__main__":
    main()
