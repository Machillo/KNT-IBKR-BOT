from __future__ import annotations

from dataclasses import dataclass

from market.discovery import ScannerPlan


@dataclass(frozen=True)
class UniversePlan:
    """Defines a dynamic opportunity surface, never a ticker whitelist."""

    name: str
    scanners: tuple[ScannerPlan, ...]


US_STOCK_OPPORTUNITY_UNIVERSE = UniversePlan(
    name="US_STOCK_OPPORTUNITY_UNIVERSE",
    scanners=(
        ScannerPlan("US_MOST_ACTIVE", "STK", "STK.US.MAJOR", "MOST_ACTIVE"),
        ScannerPlan("US_TOP_GAINERS", "STK", "STK.US.MAJOR", "TOP_PERC_GAIN"),
        ScannerPlan("US_TOP_LOSERS", "STK", "STK.US.MAJOR", "TOP_PERC_LOSE"),
        ScannerPlan("US_HOT_BY_VOLUME", "STK", "STK.US.MAJOR", "HOT_BY_VOLUME"),
    ),
)


def default_universe_plans() -> tuple[UniversePlan, ...]:
    """Initial broker-native universe registry.

    The registry is intentionally asset-aware. Additional FX/futures/options universes are
    added only when their contract discovery and risk models are implemented correctly.
    """
    return (US_STOCK_OPPORTUNITY_UNIVERSE,)
