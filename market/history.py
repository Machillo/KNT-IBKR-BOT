from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re

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


_BAR_UNITS = {"sec": 1, "secs": 1, "min": 60, "mins": 60, "hour": 3600, "hours": 3600,
              "day": 86400, "days": 86400, "week": 604800, "weeks": 604800}


def bar_size_seconds(bar_size: str) -> int | None:
    match = re.fullmatch(r"\s*(\d+)\s+([a-zA-Z]+)\s*", bar_size or "")
    if not match:
        return None
    unit = _BAR_UNITS.get(match.group(2).lower())
    return None if unit is None else int(match.group(1)) * unit


def drop_incomplete_last_bar(bars: list[PriceBar], bar_size: str, now: datetime | None = None) -> list[PriceBar]:
    """Remove a still-forming final bar so live decisions match backtest semantics.

    Backtests only ever see completed bars. If the last bar's completion cannot be
    proven (unknown size or timezone-naive time), it is dropped: using one bar of
    older, complete data is safe; acting on a partial bar is not.
    """
    if not bars:
        return bars
    seconds = bar_size_seconds(bar_size)
    last_time = bars[-1].time
    if seconds is None or not isinstance(last_time, datetime) or last_time.tzinfo is None:
        return bars[:-1]
    current = now or datetime.now(timezone.utc)
    if last_time + timedelta(seconds=seconds) > current:
        return bars[:-1]
    return bars


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
        complete_only: bool = False,
    ) -> list[PriceBar]:
        async def request():
            return await self.ib.reqHistoricalDataAsync(
                contract,
                endDateTime="",
                durationStr=duration,
                barSizeSetting=bar_size,
                whatToShow=what_to_show,
                useRTH=use_rth,
                # formatDate=2 returns timezone-aware UTC times for intraday bars.
                formatDate=2 if complete_only else 1,
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
        if complete_only:
            bars = drop_incomplete_last_bar(bars, bar_size)
        logger.info(
            "HISTORY | symbol=%s bars=%s duration=%s bar_size=%s",
            getattr(contract, "symbol", "?"), len(bars), duration, bar_size,
        )
        return bars
