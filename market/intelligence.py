from __future__ import annotations

from core.market_data import MarketDataService
from market.discovery import IBKRDiscoveryService, ScannerPlan
from market.ranker import LiquidityRanker, RankedCandidate
from market.universe import US_STOCK_OPPORTUNITY_UNIVERSE, UniversePlan
from utils.logger import logger


class MarketIntelligenceService:
    """Read-only discovery -> cheap shortlist -> market data -> execution-quality ranking."""

    def __init__(self, ib, market_data: MarketDataService) -> None:
        self.discovery = IBKRDiscoveryService(ib)
        self.market_data = market_data
        self.ranker = LiquidityRanker()

    @staticmethod
    def _discovery_shortlist(candidates, quote_budget: int):
        """Prioritize broker rank before spending market-data requests.

        This is not a ticker filter: every cycle starts from fresh scanner output. The budget
        exists to respect IBKR pacing/data-line constraints while scanning a much broader
        opportunity surface than the deep-analysis set.
        """
        ordered = sorted(candidates, key=lambda c: (c.rank, c.symbol))
        return ordered[:quote_budget]

    async def ranked_candidates(
        self,
        plans: list[ScannerPlan],
        rows_per_plan: int = 25,
        quote_budget: int = 40,
    ) -> list[RankedCandidate]:
        self.market_data.configure()
        candidates = await self.discovery.scan_many(plans, rows_per_plan)
        shortlist = self._discovery_shortlist(candidates, quote_budget)
        logger.info(
            "UNIVERSE FUNNEL | scanners=%s discovered_unique=%s quote_budget=%s quoted=%s",
            len(plans), len(candidates), quote_budget, len(shortlist),
        )

        evaluations: list[RankedCandidate] = []
        # Sequential subscriptions are deliberately conservative with IBKR pacing/data lines.
        for candidate in shortlist:
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
                for item in eligible[:10]
            ],
        )
        return ranked

    async def ranked_universe(
        self,
        universe: UniversePlan,
        rows_per_plan: int = 25,
        quote_budget: int = 40,
    ) -> list[RankedCandidate]:
        logger.info(
            "UNIVERSE DISCOVERY | universe=%s scanners=%s rows_per_scanner=%s",
            universe.name, len(universe.scanners), rows_per_plan,
        )
        return await self.ranked_candidates(
            list(universe.scanners), rows_per_plan=rows_per_plan, quote_budget=quote_budget
        )

    async def ranked_us_stocks(self, rows: int = 10) -> list[RankedCandidate]:
        """Backward-compatible narrow probe used by existing tests/tools."""
        return await self.ranked_candidates(
            [ScannerPlan("US_MOST_ACTIVE", "STK", "STK.US.MAJOR", "MOST_ACTIVE")],
            rows_per_plan=rows,
            quote_budget=rows,
        )

    async def ranked_us_opportunity_universe(
        self, rows_per_plan: int = 25, quote_budget: int = 40
    ) -> list[RankedCandidate]:
        return await self.ranked_universe(
            US_STOCK_OPPORTUNITY_UNIVERSE,
            rows_per_plan=rows_per_plan,
            quote_budget=quote_budget,
        )
