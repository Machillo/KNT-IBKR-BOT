from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DiscoveryCandidate:
    rank: int
    symbol: str
    sec_type: str
    exchange: str
    currency: str
    contract: object
    # Every (scanner name, rank) that returned this contract in the cycle.
    sources: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True)
class FunnelRow:
    """Point-in-time record of one discovered contract and what happened to it.

    status: subtype_rejected | not_quoted_budget | quote_error | ranked_eligible | ranked_rejected
    """

    symbol: str
    con_id: int
    sec_type: str
    exchange: str
    currency: str
    best_rank: int
    sources: tuple[tuple[str, int], ...]
    status: str
    reason: str
    reference_price: float | None = None
    spread_bps: float | None = None
    volume: float | None = None
    liquidity_score: float | None = None


@dataclass(frozen=True)
class RankedCandidate:
    symbol: str
    scanner_rank: int
    score: float
    eligible: bool
    reference_price: float | None
    spread_bps: float | None
    volume: float | None
    reason: str
    contract: object
