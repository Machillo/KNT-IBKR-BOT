from __future__ import annotations

from dataclasses import dataclass, field, replace
from os import getenv
from pathlib import Path

from dotenv import load_dotenv

# Absolute, repo-anchored .env: loading AND the "ACK must not be persisted" refusals read the same
# file, whatever the current directory is.
ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
load_dotenv(ENV_FILE)


def persisted_env() -> dict:
    """Values stored in the repo .env (never the process environment)."""
    from dotenv import dotenv_values

    return dict(dotenv_values(ENV_FILE)) if ENV_FILE.exists() else {}


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
    allow_live_trading: bool = field(default_factory=lambda: env_bool("ALLOW_LIVE_TRADING", False))
    readonly: bool = field(default_factory=lambda: env_bool("IBKR_READONLY", False))
    connection_timeout: float = field(default_factory=lambda: float(getenv("IBKR_CONNECTION_TIMEOUT", "10")))
    reconnect_delay: float = field(default_factory=lambda: float(getenv("IBKR_RECONNECT_DELAY", "5")))
    max_reconnect_attempts: int = field(default_factory=lambda: int(getenv("IBKR_MAX_RECONNECT_ATTEMPTS", "10")))
    paper_ports: tuple[int, ...] = (7497, 4002)
    live_ports: tuple[int, ...] = (7496, 4001)

    def validate(self) -> None:
        if self.port in self.live_ports and not self.allow_live_trading:
            raise RuntimeError(f"LIVE IBKR port {self.port} is blocked. Use Paper Trading (7497 TWS / 4002 Gateway).")
        if self.connection_timeout <= 0 or self.reconnect_delay <= 0:
            raise ValueError("IBKR timeouts/delays must be > 0")
        if self.max_reconnect_attempts < 1:
            raise ValueError("IBKR_MAX_RECONNECT_ATTEMPTS must be >= 1")


@dataclass(frozen=True)
class RiskConfig:
    # Hard per-trade risk ceiling. 1 % = what the allocator targets, so the hard layer enforces
    # the same number instead of a 10x looser one (tightening only; never relax).
    max_trade_risk_pct: float = field(default_factory=lambda: float(getenv("MAX_TRADE_RISK_PCT", "0.01")))
    max_daily_loss_pct: float = field(default_factory=lambda: float(getenv("MAX_DAILY_LOSS_PCT", "0.10")))
    max_position_pct: float = field(default_factory=lambda: float(getenv("MAX_POSITION_PCT", "0.10")))
    kill_switch_enabled: bool = field(default_factory=lambda: env_bool("KILL_SWITCH_ENABLED", True))
    kill_switch_dry_run: bool = field(default_factory=lambda: env_bool("KILL_SWITCH_DRY_RUN", True))
    # Multi-day drawdown from the persisted high-water mark that locks new entries (sticky).
    max_drawdown_pct: float = field(default_factory=lambda: float(getenv("MAX_DRAWDOWN_PCT", "0.15")))

    def validate(self) -> None:
        for name, value in (("MAX_TRADE_RISK_PCT", self.max_trade_risk_pct), ("MAX_DAILY_LOSS_PCT", self.max_daily_loss_pct), ("MAX_POSITION_PCT", self.max_position_pct), ("MAX_DRAWDOWN_PCT", self.max_drawdown_pct)):
            if not 0 < value <= 1:
                raise ValueError(f"{name} must be between 0 and 1")
        if self.max_drawdown_pct > 0.5:
            raise ValueError("MAX_DRAWDOWN_PCT above 0.5 would effectively disable the drawdown lock")


@dataclass(frozen=True)
class MarketDataConfig:
    market_data_type: int = field(default_factory=lambda: int(getenv("MARKET_DATA_TYPE", "1")))

    def validate(self) -> None:
        if self.market_data_type not in {1, 2, 3, 4}:
            raise ValueError("MARKET_DATA_TYPE must be 1, 2, 3 or 4")


@dataclass(frozen=True)
class RuntimeConfig:
    supervisor_poll_seconds: float = field(default_factory=lambda: float(getenv("SUPERVISOR_POLL_SECONDS", "15")))
    require_flat_startup: bool = field(default_factory=lambda: env_bool("REQUIRE_FLAT_STARTUP", True))
    run_broker_smoke_tests: bool = field(default_factory=lambda: env_bool("RUN_BROKER_SMOKE_TESTS", False))
    run_discovery_probe: bool = field(default_factory=lambda: env_bool("RUN_DISCOVERY_PROBE", False))
    discovery_rows: int = field(default_factory=lambda: int(getenv("DISCOVERY_ROWS", "25")))
    universe_quote_budget: int = field(default_factory=lambda: int(getenv("UNIVERSE_QUOTE_BUDGET", "40")))
    shadow_max_candidates: int = field(default_factory=lambda: int(getenv("SHADOW_MAX_CANDIDATES", "12")))
    autonomous_trading_enabled: bool = field(default_factory=lambda: env_bool("AUTONOMOUS_TRADING_ENABLED", False))
    shadow_trading_enabled: bool = field(default_factory=lambda: env_bool("SHADOW_TRADING_ENABLED", True))
    shadow_interval_seconds: float = field(default_factory=lambda: float(getenv("SHADOW_INTERVAL_SECONDS", "900")))

    def validate(self) -> None:
        if self.supervisor_poll_seconds < 1:
            raise ValueError("SUPERVISOR_POLL_SECONDS must be >= 1")
        if not 1 <= self.discovery_rows <= 50:
            raise ValueError("DISCOVERY_ROWS must be between 1 and 50")
        if not 1 <= self.universe_quote_budget <= 200:
            raise ValueError("UNIVERSE_QUOTE_BUDGET must be between 1 and 200")
        if not 1 <= self.shadow_max_candidates <= self.universe_quote_budget:
            raise ValueError("SHADOW_MAX_CANDIDATES must be >=1 and <= UNIVERSE_QUOTE_BUDGET")
        if self.shadow_interval_seconds < 60:
            raise ValueError("SHADOW_INTERVAL_SECONDS must be >= 60")


@dataclass(frozen=True)
class BotConfig:
    ibkr: IBKRConfig = field(default_factory=IBKRConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    market_data: MarketDataConfig = field(default_factory=MarketDataConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)

    def validate(self) -> None:
        self.ibkr.validate(); self.risk.validate(); self.market_data.validate(); self.runtime.validate()


config = BotConfig()


def read_only_ibkr_settings(settings: IBKRConfig, client_id_offset: int) -> IBKRConfig:
    """Settings for research/maintenance runners that must never trade.

    ``readonly=True`` is enforced by KNT itself: ``core/paper_guard`` refuses to authorize any
    order on a readonly session (ib_async's flag alone only skips order syncing; true API
    read-only mode is a TWS/Gateway setting). A separate clientId avoids colliding with — or
    impersonating — the trading bot's session.
    """
    return replace(settings, readonly=True, client_id=settings.client_id + int(client_id_offset))


# Absolute, repo-anchored runtime state directory: persisted risk locks must not depend on the
# current working directory (starting from another folder would silently start fresh).
STATE_DIR = Path(__file__).resolve().parents[1] / "state"


REPORTS_DIR = Path(__file__).resolve().parents[1] / "reports"


def reports_path(*parts: str) -> Path:
    """Path inside the repo-anchored reports directory (never CWD-relative)."""
    return REPORTS_DIR.joinpath(*parts)


def shadow_journal_path() -> Path:
    """The forward-evidence journal written by run_shadow_only.py."""
    return state_path("shadow_only", "strategy_performance.db")


def state_path(*parts: str) -> Path:
    """Path inside the runtime state directory, resolved at call time (never CWD-relative)."""
    import config.config as module  # late lookup so an override of STATE_DIR is honoured

    return Path(module.STATE_DIR).joinpath(*parts)
