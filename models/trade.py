from dataclasses import dataclass


@dataclass(frozen=True)
class TradeIntent:
    symbol: str
    action: str
    quantity: float
    entry_price: float
    stop_price: float
    order_type: str = "LIMIT"
