"""Score journaled shadow decisions (TRADE and NO_TRADE) against later bars.

Read-only. ``--source cache`` (default) uses reports/history_cache; ``--source ibkr``
requests historical bars from IBKR (read-only history requests, no order code).
Safe to re-run: FINAL outcomes are skipped, PENDING ones are retried.
"""
from __future__ import annotations

import argparse
import asyncio
import json

from research.shadow_scoring import CacheBarsProvider, ShadowScorer


class IBKRHistoryBarsProvider:
    """Read-only: completed bars after the decision via reqHistoricalData."""

    def __init__(self, ib) -> None:
        from market.history import HistoricalDataService
        from ib_async import Contract

        self.history = HistoricalDataService(ib)
        self.contract_cls = Contract

    async def bars_after(self, symbol, con_id, after, timeframe):
        from research.shadow_scoring import _naive

        if not con_id:
            return []
        contract = self.contract_cls(conId=int(con_id), exchange="SMART")
        bars = await self.history.bars(contract, duration="30 D", bar_size=timeframe or "1 hour",
                                       complete_only=True)
        cutoff = _naive(after)
        return [b for b in bars if cutoff is not None and _naive(b.time) > cutoff]


async def main_async(args) -> None:
    scorer = ShadowScorer(args.db)
    if args.source == "ibkr":
        from config.config import config
        from core.connection import IBKRConnection

        connection = IBKRConnection(config.ibkr)
        try:
            ib = await connection.connect()
            counts = await scorer.score_pending(IBKRHistoryBarsProvider(ib), limit=args.limit)
        finally:
            await connection.disconnect()
    else:
        counts = await scorer.score_pending(CacheBarsProvider(args.cache_dir), limit=args.limit)
    print(f"SHADOW SCORING | scored={counts}")
    print(json.dumps(scorer.report(), indent=2, default=str))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="state/strategy_performance.db")
    ap.add_argument("--source", choices=["cache", "ibkr"], default="cache")
    ap.add_argument("--cache-dir", default="reports/history_cache")
    ap.add_argument("--limit", type=int, default=None)
    asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    main()
