from __future__ import annotations

from dataclasses import dataclass, field
from os import getenv

from dotenv import load_dotenv

load_dotenv()


def env_bool(name: str, default: bool = False) -> bool:
    value = getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class IBKRConfig:
    host: str = field(default_factory=lambda: getenv("IBKR_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(getenv("IBKR_PORT", "4002")))
    client_id: int = field(default_factory=lambda: int(getenv("IBKR_CLIENT_ID", "901")))
    account: str | None = field(default_factory=lambda: getenv("IBKR_ACCOUNT") or None)

    # Development safety lock. Keep False until the project has completed Paper tests.
    allow_live_trading: bool = field(default_factory=lambda: env_bool("ALLOW_LIVE_TRADING", False))
    readonly: bool = field(default_factory=lambda: env_bool("IBKR_READONLY", False))

    connection_timeout: float = field(
        default_factory=lambda: float(getenv("IBKR_CONNECTION_TIMEOUT", "10"))
    )
    reconnect_delay: float = field(
        default_factory=lambda: float(getenv("IBKR_RECONNECT_DELAY", "5"))
    )
    max_reconnect_attempts: int = field(
        default_factory=lambda: int(getenv("IBKR_MAX_RECONNECT_ATTEMPTS", "10"))
    )

    # Standard IBKR ports. Custom ports can still be used, but LIVE remains locked.
    paper_ports: tuple[int, ...] = (7497, 4002)
    live_ports: tuple[int, ...] = (7496, 4001)

    def validate(self) -> None:
        if self.port in self.live_ports and not self.allow_live_trading:
            raise RuntimeError(
                f"LIVE IBKR port {self.port} is blocked. "
                "Use Paper Trading (7497 TWS / 4002 Gateway)."
            )
        if self.connection_timeout <= 0:
            raise ValueError("IBKR_CONNECTION_TIMEOUT must be > 0")
        if self.reconnect_delay <= 0:
            raise ValueError("IBKR_RECONNECT_DELAY must be > 0")
        if self.max_reconnect_attempts < 1:
            raise ValueError("IBKR_MAX_RECONNECT_ATTEMPTS must be >= 1")


@dataclass(frozen=True)
class RiskConfig:
    max_trade_risk_pct: float = field(
        default_factory=lambda: float(getenv("MAX_TRADE_RISK_PCT", "0.10"))
    )
    max_daily_loss_pct: float = field(
        default_factory=lambda: float(getenv("MAX_DAILY_LOSS_PCT", "0.10"))
    )
    max_position_pct: float = field(
        default_factory=lambda: float(getenv("MAX_POSITION_PCT", "0.10"))
    )
    kill_switch_enabled: bool = field(
        default_factory=lambda: env_bool("KILL_SWITCH_ENABLED", True)
    )
    kill_switch_dry_run: bool = field(
        default_factory=lambda: env_bool("KILL_SWITCH_DRY_RUN", True)
    )

    def validate(self) -> None:
        for name, value in (
            ("MAX_TRADE_RISK_PCT", self.max_trade_risk_pct),
            ("MAX_DAILY_LOSS_PCT", self.max_daily_loss_pct),
            ("MAX_POSITION_PCT", self.max_position_pct),
        ):
            if not 0 < value <= 1:
                raise ValueError(f"{name} must be between 0 and 1")


@dataclass(frozen=True)
class MarketDataConfig:
    market_data_type: int = field(
        default_factory=lambda: int(getenv("MARKET_DATA_TYPE", "1"))
    )

    def validate(self) -> None:
        if self.market_data_type not in {1, 2, 3, 4}:
            raise ValueError("MARKET_DATA_TYPE must be 1, 2, 3 or 4")


@dataclass(frozen=True)
class RuntimeConfig:
    supervisor_poll_seconds: float = field(
        default_factory=lambda: float(getenv("SUPERVISOR_POLL_SECONDS", "15"))
    )
    require_flat_startup: bool = field(
        default_factory=lambda: env_bool("REQUIRE_FLAT_STARTUP", True)
    )
    run_broker_smoke_tests: bool = field(
        default_factory=lambda: env_bool("RUN_BROKER_SMOKE_TESTS", False)
    )
    run_discovery_probe: bool = field(
        default_factory=lambda: env_bool("RUN_DISCOVERY_PROBE", False)
    )
    discovery_rows: int = field(
        default_factory=lambda: int(getenv("DISCOVERY_ROWS", "10"))
    )
    autonomous_trading_enabled: bool = field(
        default_factory=lambda: env_bool("AUTONOMOUS_TRADING_ENABLED", False)
    )

    def validate(self) -> None:
        if self.supervisor_poll_seconds < 1:
            raise ValueError("SUPERVISOR_POLL_SECONDS must be >= 1")
        if not 1 <= self.discovery_rows <= 50:
            raise ValueError("DISCOVERY_ROWS must be between 1 and 50")


@dataclass(frozen=True)
class BotConfig:
    ibkr: IBKRConfig = field(default_factory=IBKRConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    market_data: MarketDataConfig = field(default_factory=MarketDataConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)

    def validate(self) -> None:
        self.ibkr.validate()
        self.risk.validate()
        self.market_data.validate()
        self.runtime.validate()


config = BotConfig()
