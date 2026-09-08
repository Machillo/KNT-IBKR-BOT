from __future__ import annotations

from abc import ABC, abstractmethod


class BaseStrategy(ABC):
    """Strategies produce decisions; they do not bypass the risk/order layers."""

    @abstractmethod
    async def run_once(self) -> None:
        raise NotImplementedError
