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
    """Read-only: completed bars after the decision via reqHistoricalData.

    One request per (conId, timeframe) — later rows reuse the cached series unless they need
    an OLDER start — and at least ``min_interval_seconds`` between requests (IBKR pacing:
    60 historical requests per 10 minutes).
    """

    name = "IBKR"
    authoritative = True

    def __init__(self, ib, min_interval_seconds: float = 10.5) -> None:
        from market.history import HistoricalDataService
        from ib_async import Contract

        self.history = HistoricalDataService(ib)
        self.contract_cls = Contract
        self.min_interval = max(0.0, float(min_interval_seconds))
        self._cache: dict[tuple[int, str], tuple[object, list]] = {}
        self._last_request: float | None = None

    async def _paced(self, coro_factory):
        import time

        if self._last_request is not None:
            wait = self.min_interval - (time.monotonic() - self._last_request)
            if wait > 0:
                await asyncio.sleep(wait)
        self._last_request = time.monotonic()
        return await coro_factory()

    async def bars_from(self, symbol, con_id, at, timeframe):
        """Bars from the decision bar onward; the request span covers the decision's age so the
        decision bar is included (the scorer refuses series that do not start with it)."""
        from datetime import datetime

        from research.shadow_scoring import _naive

        if not con_id:
            return []
        start = _naive(at)
        if start is None:
            return []
        key = (int(con_id), timeframe or "1 hour")
        cached = self._cache.get(key)
        if cached is None or cached[0] > start:
            age_days = (datetime.utcnow() - start).days
            days = min(365, max(2, age_days + 3))
            contract = self.contract_cls(conId=int(con_id), exchange="SMART")
            bars = await self._paced(lambda: self.history.bars(contract, duration=f"{days} D",
                                                               bar_size=key[1], complete_only=True))
            first = _naive(bars[0].time) if bars else start
            self._cache[key] = (first if first is not None else start, list(bars))
            cached = self._cache[key]
        return [b for b in cached[1] if _naive(b.time) >= start]


async def main_async(args) -> None:
    from config.config import shadow_journal_path

    scorer = ShadowScorer(args.db or shadow_journal_path())
    if args.source == "ibkr":
        from config.config import config, read_only_ibkr_settings
        from core.connection import IBKRConnection

        connection = IBKRConnection(read_only_ibkr_settings(config.ibkr, 50))
        try:
            ib = await connection.connect()
            counts = await scorer.score_pending(IBKRHistoryBarsProvider(ib), limit=args.limit)
        finally:
            await connection.disconnect()
    else:
        from config.config import reports_path

        # Forward rows live in the journal-bar cache (run_fetch_journal_bars.py); the old
        # history cache is the closed v1 holdout and never contains forward bars.
        cache_dir = args.cache_dir or (reports_path("pit_cache") if args.source == "pit" else None)
        counts = await scorer.score_pending(CacheBarsProvider(cache_dir), limit=args.limit)
    print(f"SHADOW SCORING | scored={counts}")
    print(json.dumps(scorer.report(), indent=2, default=str))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=None, help="default: the shadow-only journal <repo>/state/shadow_only/strategy_performance.db")
    ap.add_argument("--source", choices=["pit", "cache", "ibkr"], default="pit",
                    help="pit = reports/pit_cache (journal bars, default); cache = reports/history_cache; "
                         "ibkr = read-only history (the only source allowed to expire unalignable rows)")
    ap.add_argument("--cache-dir", default=None, help="override the cache directory")
    ap.add_argument("--limit", type=int, default=None)
    asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    main()
