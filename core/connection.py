from __future__ import annotations

import asyncio

from ib_async import IB

from config.config import IBKRConfig
from core.exceptions import IBKRConnectionError
from utils.logger import logger


class IBKRConnection:
    """Owns the IB connection and reconnect lifecycle."""

    def __init__(self, settings: IBKRConfig) -> None:
        self.settings = settings
        self.ib = IB()
        self._stopping = False
        self._reconnect_task: asyncio.Task | None = None
        self._attach_events()

    def _attach_events(self) -> None:
        self.ib.connectedEvent += self._on_connected
        self.ib.disconnectedEvent += self._on_disconnected
        self.ib.errorEvent += self._on_error

    async def connect(self) -> IB:
        self.settings.validate()
        if self.ib.isConnected():
            return self.ib

        try:
            logger.info(
                "Connecting to IBKR host=%s port=%s clientId=%s readonly=%s",
                self.settings.host,
                self.settings.port,
                self.settings.client_id,
                self.settings.readonly,
            )
            await self.ib.connectAsync(
                self.settings.host,
                self.settings.port,
                clientId=self.settings.client_id,
                timeout=self.settings.connection_timeout,
                readonly=self.settings.readonly,
                account=self.settings.account or "",
            )
        except Exception as exc:
            raise IBKRConnectionError(f"Unable to connect to IBKR: {exc}") from exc

        if not self.ib.isConnected():
            raise IBKRConnectionError("IBKR connection did not become active")
        return self.ib

    def _on_connected(self) -> None:
        logger.info("IBKR connected")

    def _on_disconnected(self) -> None:
        logger.warning("IBKR disconnected")
        if self._stopping:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.error("No active asyncio loop; automatic reconnect could not start")
            return

        if self._reconnect_task is None or self._reconnect_task.done():
            self._reconnect_task = loop.create_task(self._reconnect_loop())

    def _on_error(self, req_id: int, error_code: int, error_string: str, contract=None) -> None:
        informational_codes = {202, 2104, 2106, 2107, 2108, 2158, 10167, 10349}
        warning_codes = {1100, 1101, 1102}
        scanner_cancelled = error_code == 162 and "scanner subscription cancelled" in error_string.lower()

        if error_code in informational_codes or scanner_cancelled:
            log = logger.info
        elif error_code in warning_codes:
            log = logger.warning
        else:
            log = logger.error

        log(
            "IBKR event reqId=%s code=%s message=%s contract=%s",
            req_id,
            error_code,
            error_string,
            getattr(contract, "localSymbol", None),
        )

    async def _reconnect_loop(self) -> None:
        for attempt in range(1, self.settings.max_reconnect_attempts + 1):
            if self._stopping or self.ib.isConnected():
                return
            logger.warning(
                "Reconnect attempt %s/%s in %.1fs",
                attempt, self.settings.max_reconnect_attempts, self.settings.reconnect_delay,
            )
            await asyncio.sleep(self.settings.reconnect_delay)
            try:
                await self.connect()
                if self.ib.isConnected():
                    logger.info("IBKR reconnection successful")
                    return
            except Exception as exc:
                logger.error("Reconnect attempt failed: %s", exc)
        logger.critical("IBKR reconnect attempts exhausted")

    async def disconnect(self) -> None:
        self._stopping = True
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
        if self.ib.isConnected():
            self.ib.disconnect()
        logger.info("IBKR connection closed")
