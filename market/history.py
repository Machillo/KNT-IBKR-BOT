from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

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
    """Small IBKR historical-data adapter used by strategies and backtests."""

    def __init__(self, ib) -> None:
        self.ib = ib

    async def bars(
        self,
        contract,
        duration: str = "30 D",
        bar_size: str = "1 hour",
        what_to_show: str = "TRADES",
        use_rth: bool = True,
    ) -> list[PriceBar]:
        raw = await self.ib.reqHistoricalDataAsync(
            contract,
            endDateTime="",
            durationStr=duration,
            barSizeSetting=bar_size,
            whatToShow=what_to_show,
            useRTH=use_rth,
            formatDate=1,
            keepUpToDate=False,
        )
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
