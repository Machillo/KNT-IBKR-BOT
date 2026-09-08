class TradingBotError(Exception):
    """Base exception for the trading bot."""


class IBKRConnectionError(TradingBotError):
    """Raised when the IBKR connection cannot be established."""


class RiskRejectedError(TradingBotError):
    """Raised when an order is rejected by the risk layer."""


class KillSwitchTriggered(TradingBotError):
    """Raised when emergency shutdown has been activated."""
