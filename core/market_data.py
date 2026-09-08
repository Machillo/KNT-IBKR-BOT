from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass

from ib_async import Forex, IB, Stock, Ticker

from config.config import MarketDataConfig
from utils.logger import logger


@dataclass(frozen=True)
class MarketSnapshot:
    symbol: str
    bid: float | None
    ask: float | None
    last: float | None
    market_price: float | None
    volume: float | None = None

    @property
    def has_price(self) -> bool:
        return any(
            value is not None
            for value in (self.bid, self.ask, self.last, self.market_price)
        )

    @property
    def spread_bps(self) -> float | None:
        if self.bid is None or self.ask is None or self.bid <= 0 or self.ask < self.bid:
            return None
        mid = (self.bid + self.ask) / 2
        return ((self.ask - self.bid) / mid) * 10_000 if mid > 0 else None


class MarketDataService:
    def __init__(self, ib: IB, settings: MarketDataConfig) -> None:
        self.ib = ib
        self.settings = settings

    def configure(self, market_data_type: int | None = None) -> None:
        """Configure IBKR market-data mode.

        1 = live, 2 = frozen, 3 = delayed, 4 = delayed-frozen.
        """
        self.settings.validate()
        selected = self.settings.market_data_type if market_data_type is None else market_data_type
        if selected not in {1, 2, 3, 4}:
            raise ValueError("market_data_type must be 1, 2, 3 or 4")

        self.ib.reqMarketDataType(selected)
        logger.info("Market data type configured: %s", selected)

    async def stock(
        self,
        symbol: str,
        exchange: str = "SMART",
        currency: str = "USD",
    ) -> Ticker:
        contract = Stock(symbol.upper(), exchange, currency)
        qualified = await self.ib.qualifyContractsAsync(contract)
        if not qualified:
            raise RuntimeError(f"Could not qualify stock/ETF contract: {symbol}")

        contract = qualified[0]
        logger.info("Subscribing stock/ETF market data: %s", symbol.upper())
        return self.ib.reqMktData(contract, "", False, False)

    async def forex(self, pair: str) -> Ticker:
        normalized = pair.replace("/", "").upper()
        contract = Forex(normalized)
        qualified = await self.ib.qualifyContractsAsync(contract)
        if not qualified:
            raise RuntimeError(f"Could not qualify Forex contract: {pair}")

        contract = qualified[0]
        logger.info("Subscribing Forex market data: %s", normalized)
        return self.ib.reqMktData(contract, "", False, False)

    @staticmethod
    def _clean(value: float | None) -> float | None:
        if value is None:
            return None
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return None
        return numeric if math.isfinite(numeric) else None

    async def wait_for_snapshot(
        self,
        ticker: Ticker,
        symbol: str,
        timeout: float = 8.0,
    ) -> MarketSnapshot:
        deadline = asyncio.get_running_loop().time() + timeout

        while asyncio.get_running_loop().time() < deadline:
            snapshot = MarketSnapshot(
                symbol=symbol.upper(),
                bid=self._clean(ticker.bid),
                ask=self._clean(ticker.ask),
                last=self._clean(ticker.last),
                market_price=self._clean(ticker.marketPrice()),
                volume=self._clean(getattr(ticker, "volume", None)),
            )
            if snapshot.has_price:
                return snapshot
            await asyncio.sleep(0.25)

        return MarketSnapshot(
            symbol=symbol.upper(),
            bid=self._clean(ticker.bid),
            ask=self._clean(ticker.ask),
            last=self._clean(ticker.last),
            market_price=self._clean(ticker.marketPrice()),
            volume=self._clean(getattr(ticker, "volume", None)),
        )

    async def snapshot_contract(
        self, contract, symbol: str | None = None, timeout: float = 5.0
    ) -> MarketSnapshot:
        label = (symbol or contract.localSymbol or contract.symbol or str(contract.conId)).upper()
        ticker = self.ib.reqMktData(contract, "", False, False)
        try:
            return await self.wait_for_snapshot(ticker, label, timeout=timeout)
        finally:
            self.ib.cancelMktData(contract)

    async def _test_subscription(self, ticker: Ticker, symbol: str) -> MarketSnapshot:
        try:
            snapshot = await self.wait_for_snapshot(ticker, symbol)
            logger.info(
                "MARKET DATA | symbol=%s bid=%s ask=%s last=%s market_price=%s",
                snapshot.symbol,
                snapshot.bid,
                snapshot.ask,
                snapshot.last,
                snapshot.market_price,
            )
            if not snapshot.has_price:
                logger.warning("No usable market price received for %s", symbol.upper())
            return snapshot
        finally:
            # reqMktData above creates a streaming subscription, so cancel exactly once.
            # With delayed data enabled the request remains valid even without a paid
            # US equities feed, avoiding the previous 10089 -> duplicate-cancel noise.
            self.ib.cancelMktData(ticker.contract)
            logger.info("Market data subscription cancelled: %s", symbol.upper())

    async def test_stock_snapshot(self, symbol: str = "SPY") -> MarketSnapshot:
        ticker = await self.stock(symbol)
        return await self._test_subscription(ticker, symbol)

    async def test_forex_snapshot(self, pair: str = "EURUSD") -> MarketSnapshot:
        ticker = await self.forex(pair)
        return await self._test_subscription(ticker, pair.replace("/", ""))
