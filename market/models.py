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
