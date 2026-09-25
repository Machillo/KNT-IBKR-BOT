from __future__ import annotations

from dataclasses import dataclass, replace

from ib_async import IB, ScannerSubscription

from market.models import DiscoveryCandidate
from utils.logger import logger


@dataclass(frozen=True)
class ScannerPlan:
    name: str
    instrument: str
    location_code: str
    scan_code: str = "MOST_ACTIVE"


class IBKRDiscoveryService:
    """Broker-native, ticker-agnostic discovery.

    Scanner plans describe market segments, never permanent symbol whitelists. New IBKR
    instrument/location combinations can be added without changing strategy code.
    """

    def __init__(self, ib: IB) -> None:
        self.ib = ib

    async def scan(self, plan: ScannerPlan, rows: int = 10) -> list[DiscoveryCandidate]:
        subscription = ScannerSubscription(
            instrument=plan.instrument,
            locationCode=plan.location_code,
            scanCode=plan.scan_code,
            numberOfRows=rows,
        )
        data = await self.ib.reqScannerDataAsync(subscription)
        candidates: list[DiscoveryCandidate] = []
        for item in data:
            contract = item.contractDetails.contract
            candidates.append(
                DiscoveryCandidate(
                    rank=int(item.rank),
                    symbol=contract.localSymbol or contract.symbol,
                    sec_type=contract.secType,
                    exchange=contract.primaryExchange or contract.exchange,
                    currency=contract.currency,
                    contract=contract,
                    sources=((plan.name, int(item.rank)),),
                )
            )
        logger.info(
            "DISCOVERY | adapter=%s instrument=%s location=%s requested=%s received=%s symbols=%s",
            plan.name, plan.instrument, plan.location_code, rows, len(candidates),
            [c.symbol for c in candidates],
        )
        return candidates

    async def scan_many(self, plans: list[ScannerPlan], rows_per_plan: int = 10) -> list[DiscoveryCandidate]:
        """Run multiple broker-native market segments and dedupe contracts by conId."""
        merged: dict[tuple[str, int | str], DiscoveryCandidate] = {}
        # Per-cycle record for data-quality gates: rows returned per scanner and scanners that
        # failed (a failure used to vanish into a log line).
        self.last_scan_rows: dict[str, int] = {}
        self.last_scan_errors: dict[str, str] = {}
        for plan in plans:
            try:
                results = await self.scan(plan, rows_per_plan)
                self.last_scan_rows[plan.name] = len(results)
                for candidate in results:
                    con_id = getattr(candidate.contract, "conId", 0)
                    key = (candidate.sec_type, con_id or candidate.symbol)
                    current = merged.get(key)
                    if current is None:
                        merged[key] = candidate
                    else:
                        # Keep the best rank but remember every scanner that returned it.
                        best = candidate if candidate.rank < current.rank else current
                        merged[key] = replace(best, sources=current.sources + candidate.sources)
            except Exception as exc:
                self.last_scan_errors[plan.name] = type(exc).__name__
                logger.warning("DISCOVERY adapter skipped | adapter=%s error=%s", plan.name, exc)
        return list(merged.values())

    async def scan_us_most_active(self, rows: int = 10) -> list[DiscoveryCandidate]:
        return await self.scan(
            ScannerPlan("US_MOST_ACTIVE", "STK", "STK.US.MAJOR", "MOST_ACTIVE"), rows
        )
