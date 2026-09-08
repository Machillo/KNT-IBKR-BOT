from __future__ import annotations

from core.market_data import MarketDataService
from market.discovery import IBKRDiscoveryService, ScannerPlan
from market.ranker import LiquidityRanker, RankedCandidate
from utils.logger import logger


class MarketIntelligenceService:
    """Read-only discovery -> market data -> execution-quality ranking pipeline."""

    def __init__(self, ib, market_data: MarketDataService) -> None:
        self.discovery = IBKRDiscoveryService(ib)
        self.market_data = market_data
        self.ranker = LiquidityRanker()

    async def ranked_candidates(self, plans: list[ScannerPlan], rows_per_plan: int = 10) -> list[RankedCandidate]:
        self.market_data.configure()
        candidates = await self.discovery.scan_many(plans, rows_per_plan)
        evaluations: list[RankedCandidate] = []

        # Sequential subscriptions are deliberately conservative with IBKR pacing/data lines.
        for candidate in candidates:
            try:
                snapshot = await self.market_data.snapshot_contract(
                    candidate.contract,
                    candidate.symbol,
                    timeout=3.0,
                )
                evaluations.append(self.ranker.evaluate(candidate, snapshot))
            except Exception as exc:
                logger.warning("INTELLIGENCE candidate skipped | symbol=%s error=%s", candidate.symbol, exc)

        ranked = self.ranker.rank(evaluations)
        eligible = [item for item in ranked if item.eligible]
        logger.info(
            "INTELLIGENCE RANKING | eligible=%s/%s top=%s",
            len(eligible),
            len(ranked),
            [
                {
                    "symbol": item.symbol,
                    "score": round(item.score, 2),
                    "spread_bps": None if item.spread_bps is None else round(item.spread_bps, 2),
                }
                for item in eligible[:5]
            ],
        )
        return ranked

    async def ranked_us_stocks(self, rows: int = 10) -> list[RankedCandidate]:
        return await self.ranked_candidates(
            [ScannerPlan("US_MOST_ACTIVE", "STK", "STK.US.MAJOR", "MOST_ACTIVE")],
            rows_per_plan=rows,
        )
