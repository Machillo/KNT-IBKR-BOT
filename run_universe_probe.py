from __future__ import annotations

import asyncio

from config.config import config
from core.connection import IBKRConnection
from market.discovery import IBKRDiscoveryService
from market.universe import US_STOCK_OPPORTUNITY_UNIVERSE


async def run() -> None:
    config.validate()
    connection = IBKRConnection(config.ibkr)
    try:
        ib = await connection.connect()
        service = IBKRDiscoveryService(ib)
        candidates = await service.scan_many(
            list(US_STOCK_OPPORTUNITY_UNIVERSE.scanners),
            rows_per_plan=config.runtime.discovery_rows,
        )
        ordered = sorted(candidates, key=lambda x: (x.rank, x.symbol))
        print(
            f"universe={US_STOCK_OPPORTUNITY_UNIVERSE.name} "
            f"scanners={len(US_STOCK_OPPORTUNITY_UNIVERSE.scanners)} "
            f"rows_per_scanner={config.runtime.discovery_rows} "
            f"unique_candidates={len(ordered)}"
        )
        for item in ordered:
            print(
                f"rank={item.rank:2d} symbol={item.symbol:12} secType={item.sec_type:5} "
                f"exchange={item.exchange:10} currency={item.currency}"
            )
    finally:
        await connection.disconnect()


if __name__ == "__main__":
    asyncio.run(run())
