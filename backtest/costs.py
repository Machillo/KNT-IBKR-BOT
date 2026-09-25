"""Explicit, configurable execution-cost model for the backtester.

Three components are modelled separately so each can be stressed and reported:

* commission — IBKR Pro *Fixed* US-stock schedule by default: USD 0.005 per
  share, minimum USD 1.00 per order, maximum 1 % of trade value. An optional
  percentage component exists only for legacy bps-style scenarios.
* half spread — crossing half the quoted spread on marketable orders
  (entries, stop exits, end-of-data exits). Resting limit orders (targets)
  pay no spread.
* slippage — extra adverse movement on marketable orders, in bps.

A small sell-side regulatory fee (SEC fee / FINRA TAF approximation) is applied
to shares sold. Rates change over time; the default is a conservative
approximation, not an exact current schedule.

Values are defaults for liquid US large caps on hourly-or-slower bars; stress
scenarios multiply spread and slippage. Nothing here is fitted to results.
"""
from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class CostModel:
    name: str = "ibkr_fixed_baseline"
    commission_per_share: float = 0.005
    commission_min: float = 1.00
    commission_max_pct: float = 0.01
    commission_bps: float = 0.0
    half_spread_bps: float = 1.5
    slippage_bps: float = 2.0
    sell_fee_bps: float = 0.3
    # Stock-borrow cost for shorts, annualized % of notional (easy-to-borrow large caps
    # are usually below this; hard-to-borrow names can be far higher).
    short_borrow_annual_pct: float = 1.0

    def __post_init__(self) -> None:
        for field_name in ("commission_per_share", "commission_min", "commission_max_pct",
                           "commission_bps", "half_spread_bps", "slippage_bps", "sell_fee_bps",
                           "short_borrow_annual_pct"):
            if getattr(self, field_name) < 0:
                raise ValueError(f"{field_name} must be >= 0")

    @property
    def marketable_bps(self) -> float:
        return self.half_spread_bps + self.slippage_bps

    def commission(self, quantity: float, price: float) -> float:
        quantity, price = abs(float(quantity)), float(price)
        if quantity <= 0 or price <= 0:
            return 0.0
        notional = quantity * price
        per_share = quantity * self.commission_per_share
        fixed = per_share
        if per_share > 0 or self.commission_min > 0:
            fixed = max(per_share, self.commission_min)
            if self.commission_max_pct > 0:
                fixed = min(fixed, notional * self.commission_max_pct)
        return fixed + notional * self.commission_bps / 10_000

    def sell_fee(self, quantity: float, price: float) -> float:
        return abs(quantity) * price * self.sell_fee_bps / 10_000

    def borrow_cost(self, notional: float, days: float) -> float:
        return abs(notional) * self.short_borrow_annual_pct / 100 * max(0.0, days) / 360

    def estimated_exit_cost(self, quantity: float, price: float, *, long: bool) -> float:
        """Cost to close at ``price`` with a marketable order (for mark-to-market)."""
        cost = self.commission(quantity, price) + abs(quantity) * price * self.marketable_bps / 10_000
        return cost + (self.sell_fee(quantity, price) if long else 0.0)

    def marketable_fill(self, reference: float, *, buy: bool) -> float:
        """Price paid/received by a marketable order at ``reference``."""
        adj = self.marketable_bps / 10_000
        return reference * (1 + adj) if buy else reference * (1 - adj)

    def stressed(self, multiplier: float, name: str | None = None) -> "CostModel":
        return replace(
            self,
            name=name or f"{self.name}_x{multiplier:g}",
            half_spread_bps=self.half_spread_bps * multiplier,
            slippage_bps=self.slippage_bps * multiplier,
        )

    @classmethod
    def zero(cls) -> "CostModel":
        return cls("zero_cost", 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    @classmethod
    def legacy_bps(cls, commission_bps: float, slippage_bps: float) -> "CostModel":
        """Old engine semantics: a flat bps charge per side on notional."""
        return cls("legacy_bps", 0.0, 0.0, 0.0, float(commission_bps), 0.0, float(slippage_bps), 0.0, 0.0)


BASELINE = CostModel()
STRESSED = BASELINE.stressed(2.5, "ibkr_fixed_stressed")
SEVERE = BASELINE.stressed(5.0, "ibkr_fixed_severe")
