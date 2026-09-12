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
    def _is_common_stock_candidate(candidate) -> bool:
        """Reject obvious non-common-stock wrappers surfaced by STK scanners.

        IBKR may encode rights/preferred/share-class style instruments as STK and expose
        localSymbols containing spaces. Until we have explicit asset subtype handling,
        fail closed for those candidates so they cannot contaminate stock strategy research.
        """
        symbol = (candidate.symbol or "").strip()
        if not symbol:
            return False
        if candidate.sec_type != "STK":
            return False
        if " " in symbol:
            return False
        return True

    @classmethod
    def _discovery_shortlist(cls, candidates, quote_budget: int):
        """Prioritize broker rank before spending market-data requests.

        This is not a ticker whitelist: every cycle starts from fresh scanner output. The
        budget exists to respect IBKR pacing/data-line constraints. Obvious rights/preferred
        wrappers are excluded until their own subtype-aware models exist.
        """
        filtered = [c for c in candidates if cls._is_common_stock_candidate(c)]
        ordered = sorted(filtered, key=lambda c: (c.rank, c.symbol))
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
        rejected = len(candidates) - len([c for c in candidates if self._is_common_stock_candidate(c)])
        logger.info(
            "UNIVERSE FUNNEL | scanners=%s discovered_unique=%s subtype_rejected=%s quote_budget=%s quoted=%s",
            len(plans), len(candidates), rejected, quote_budget, len(shortlist),
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
