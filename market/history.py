from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from core.pacing import AsyncPacingLimiter
from utils.logger import logger


@dataclass(frozen=True)
class PriceBar:
    time: datetime | str
    open: float
    high: float
    low: float
    close: float
    volume: float


class HistoricalDataService:
    """IBKR historical-data adapter with a bounded request queue."""

    def __init__(self, ib, pacing: AsyncPacingLimiter | None = None) -> None:
        self.ib = ib
        # History has broker-specific pacing rules beyond global request throughput;
        # keep this intentionally conservative and replaceable.
        self.pacing = pacing or AsyncPacingLimiter(max_requests=8, per_seconds=1.0)

    async def bars(
        self,
        contract,
        duration: str = "30 D",
        bar_size: str = "1 hour",
        what_to_show: str = "TRADES",
        use_rth: bool = True,
    ) -> list[PriceBar]:
        async def request():
            return await self.ib.reqHistoricalDataAsync(
                contract,
                endDateTime="",
                durationStr=duration,
                barSizeSetting=bar_size,
                whatToShow=what_to_show,
                useRTH=use_rth,
                formatDate=1,
                keepUpToDate=False,
            )

        raw = await self.pacing.run(request)
        bars = [
            PriceBar(
                time=item.date,
                open=float(item.open),
                high=float(item.high),
                low=float(item.low),
                close=float(item.close),
                volume=float(item.volume or 0),
            )
            for item in raw
            if float(item.close) > 0
        ]
        logger.info(
            "HISTORY | symbol=%s bars=%s duration=%s bar_size=%s",
            getattr(contract, "symbol", "?"), len(bars), duration, bar_size,
        )
        return bars
