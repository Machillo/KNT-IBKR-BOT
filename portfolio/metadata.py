"""Instrument metadata for portfolio concentration checks (read-only).

IBKR contract details carry ``industry`` / ``category`` / ``subcategory`` for stocks. KNT uses
``industry`` as the sector bucket for ``CrossExposureGuard``. Lookups are read-only
(``reqContractDetails``), cached per conId, and return ``None`` on any failure so the
admission layer can fail closed when sector metadata is required.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class InstrumentMetadata:
    sector: str | None
    industry_category: str | None = None

    @property
    def known(self) -> bool:
        return bool(self.sector and self.sector.strip() and self.sector.upper() != "UNKNOWN")


class ContractMetadataService:
    def __init__(self, ib) -> None:
        self.ib = ib
        self._cache: dict[int, InstrumentMetadata] = {}

    async def get(self, contract) -> InstrumentMetadata | None:
        con_id = int(getattr(contract, "conId", 0) or 0)
        if con_id and con_id in self._cache:
            return self._cache[con_id]
        request = getattr(self.ib, "reqContractDetailsAsync", None)
        if request is None:
            return None
        try:
            details = await request(contract)
        except Exception:
            return None
        if not details:
            return None
        detail = details[0]
        meta = InstrumentMetadata(
            sector=(getattr(detail, "industry", "") or "").strip() or None,
            industry_category=(getattr(detail, "category", "") or "").strip() or None,
        )
        if con_id and meta.known:
            self._cache[con_id] = meta
        return meta
