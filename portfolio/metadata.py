"""Instrument metadata for portfolio concentration checks (read-only).

IBKR contract details carry ``industry`` / ``category`` / ``subcategory`` for stocks. KNT uses
``industry`` as the sector bucket for ``CrossExposureGuard``. Lookups are read-only
(``reqContractDetails``), cached per conId, and return ``None`` on any failure so the
admission layer can fail closed when sector metadata is required.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

CACHE_TTL_SECONDS = 24 * 3600
# Instrument types the current (single-stock) strategies and risk model are built for. ETFs —
# including leveraged and inverse ones, whose risk is a multiple of the underlying — ETNs,
# closed-end funds, preferreds, warrants, rights and units are NOT analysed; an unknown type is
# refused (fail closed). Widening this set is a decision-path change (new FWD version).
TRADABLE_STOCK_TYPES = frozenset({"COMMON", "ADR", "REIT"})  # sector classifications change rarely, but never trust a stale one forever


@dataclass(frozen=True)
class InstrumentMetadata:
    sector: str | None
    industry_category: str | None = None
    stock_type: str | None = None      # IBKR ContractDetails.stockType (COMMON, ETF, ADR, REIT, ...)

    @property
    def known(self) -> bool:
        return bool(self.sector and self.sector.strip() and self.sector.upper() != "UNKNOWN")


class ContractMetadataService:
    def __init__(self, ib, ttl_seconds: float = CACHE_TTL_SECONDS, clock=time.monotonic) -> None:
        self.ib = ib
        self.ttl = float(ttl_seconds)
        self._clock = clock
        self._cache: dict[int, tuple[float, InstrumentMetadata]] = {}

    async def get(self, contract) -> InstrumentMetadata | None:
        con_id = int(getattr(contract, "conId", 0) or 0)
        cached = self._cache.get(con_id) if con_id else None
        if cached is not None and self._clock() - cached[0] < self.ttl:
            return cached[1]
        request = getattr(self.ib, "reqContractDetailsAsync", None)
        if request is None:
            return None
        try:
            details = await request(contract)
        except Exception:
            return None
        if not details:
            return None
        if len(details) != 1:
            return None  # ambiguous contract: fail closed instead of guessing a sector
        detail = details[0]
        meta = InstrumentMetadata(
            sector=(getattr(detail, "industry", "") or "").strip() or None,
            industry_category=(getattr(detail, "category", "") or "").strip() or None,
            stock_type=(str(getattr(detail, "stockType", "") or "")).strip() or None,
        )
        if con_id and meta.known:
            self._cache[con_id] = (self._clock(), meta)
        return meta
