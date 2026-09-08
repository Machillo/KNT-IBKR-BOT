from __future__ import annotations

from core.market_data import MarketSnapshot
from market.models import DiscoveryCandidate, RankedCandidate


class LiquidityRanker:
    """Ranks discovery candidates for analysis; this is not a trading strategy."""

    def __init__(self, max_spread_bps: float = 50.0, min_price: float = 1.0) -> None:
        self.max_spread_bps = max_spread_bps
        self.min_price = min_price

    def evaluate(self, candidate: DiscoveryCandidate, snapshot: MarketSnapshot) -> RankedCandidate:
        price = snapshot.market_price or snapshot.last or snapshot.ask or snapshot.bid
        spread = snapshot.spread_bps

        if price is None or price <= 0:
            return RankedCandidate(
                candidate.symbol, candidate.rank, 0.0, False, price, spread,
                snapshot.volume, "no usable price", candidate.contract,
            )
        if price < self.min_price:
            return RankedCandidate(
                candidate.symbol, candidate.rank, 0.0, False, price, spread,
                snapshot.volume, f"price below {self.min_price}", candidate.contract,
            )
        if spread is None:
            return RankedCandidate(
                candidate.symbol, candidate.rank, 0.0, False, price, spread,
                snapshot.volume, "no reliable bid/ask spread", candidate.contract,
            )
        if spread > self.max_spread_bps:
            return RankedCandidate(
                candidate.symbol, candidate.rank, 0.0, False, price, spread,
                snapshot.volume, f"spread {spread:.1f} bps too wide", candidate.contract,
            )

        # Scanner rank drives discovery quality; tighter spread improves execution quality.
        score = max(0.0, 100.0 - candidate.rank * 2.0 - min(spread, 50.0) * 0.5)
        return RankedCandidate(
            candidate.symbol,
            candidate.rank,
            score,
            True,
            price,
            spread,
            snapshot.volume,
            "eligible for strategy analysis",
            candidate.contract,
        )

    def rank(self, evaluations: list[RankedCandidate]) -> list[RankedCandidate]:
        return sorted(
            evaluations,
            key=lambda item: (item.eligible, item.score),
            reverse=True,
        )
