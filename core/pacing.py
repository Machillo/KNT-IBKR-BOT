from __future__ import annotations

import asyncio
from collections import deque
from time import monotonic


class AsyncPacingLimiter:
    """Simple sliding-window limiter for IBKR-facing request queues.

    Defaults deliberately stay below commonly encountered broker request ceilings.
    Callers can configure separate limiters for history, scanners and quotes.
    """

    def __init__(self, *, max_requests: int = 45, per_seconds: float = 1.0) -> None:
        if max_requests < 1 or per_seconds <= 0:
            raise ValueError("Invalid pacing configuration")
        self.max_requests = int(max_requests)
        self.per_seconds = float(per_seconds)
        self._timestamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = monotonic()
                cutoff = now - self.per_seconds
                while self._timestamps and self._timestamps[0] <= cutoff:
                    self._timestamps.popleft()
                if len(self._timestamps) < self.max_requests:
                    self._timestamps.append(now)
                    return
                sleep_for = self.per_seconds - (now - self._timestamps[0])
                await asyncio.sleep(max(0.001, sleep_for))

    async def run(self, awaitable_factory):
        await self.acquire()
        return await awaitable_factory()
