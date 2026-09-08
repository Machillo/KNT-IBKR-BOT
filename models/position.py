from dataclasses import dataclass


@dataclass(frozen=True)
class PositionSnapshot:
    symbol: str
    quantity: float
    average_cost: float
    market_price: float | None = None
