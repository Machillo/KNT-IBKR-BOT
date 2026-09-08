from __future__ import annotations

from ib_async import IB, ScannerSubscription

from market.models import DiscoveryCandidate
from utils.logger import logger


class IBKRDiscoveryService:
    """Broker-native discovery. First adapter is US stocks; other asset adapters plug in here."""

    def __init__(self, ib: IB) -> None:
        self.ib = ib

    async def scan_us_most_active(self, rows: int = 10) -> list[DiscoveryCandidate]:
        subscription = ScannerSubscription(
            instrument="STK",
            locationCode="STK.US.MAJOR",
            scanCode="MOST_ACTIVE",
            numberOfRows=rows,
        )
        data = await self.ib.reqScannerDataAsync(subscription)
        candidates: list[DiscoveryCandidate] = []
        for item in data:
            contract = item.contractDetails.contract
            candidates.append(
                DiscoveryCandidate(
                    rank=int(item.rank),
                    symbol=contract.symbol,
                    sec_type=contract.secType,
                    exchange=contract.primaryExchange or contract.exchange,
                    currency=contract.currency,
                    contract=contract,
                )
            )
        logger.info(
            "DISCOVERY | adapter=US_MOST_ACTIVE requested=%s received=%s symbols=%s",
            rows,
            len(candidates),
            [c.symbol for c in candidates],
        )
        return candidates
