from __future__ import annotations

from dataclasses import dataclass
from math import sqrt


@dataclass(frozen=True)
class ExposurePosition:
    symbol: str
    side: str
    notional: float
    sector: str = "UNKNOWN"
    returns: tuple[float, ...] = ()


@dataclass(frozen=True)
class ExposureDecision:
    approved: bool
    reason: str
    correlated_notional_pct: float
    same_sector_notional_pct: float
    max_abs_correlation: float


class CrossExposureGuard:
    """Reject concentrated/correlated additions before broker execution.

    The guard is deliberately independent from broker APIs so it can be tested and
    reused in shadow, paper and future asset-aware execution layers.
    """

    def __init__(
        self,
        *,
        max_same_sector_pct: float = 0.30,
        max_correlated_cluster_pct: float = 0.40,
        correlation_threshold: float = 0.80,
    ) -> None:
        self.max_same_sector_pct = float(max_same_sector_pct)
        self.max_correlated_cluster_pct = float(max_correlated_cluster_pct)
        self.correlation_threshold = float(correlation_threshold)

    @staticmethod
    def correlation(a: tuple[float, ...], b: tuple[float, ...]) -> float:
        n = min(len(a), len(b))
        if n < 20:
            return 0.0
        x, y = a[-n:], b[-n:]
        mx, my = sum(x) / n, sum(y) / n
        vx = sum((v - mx) ** 2 for v in x)
        vy = sum((v - my) ** 2 for v in y)
        if vx <= 0 or vy <= 0:
            return 0.0
        cov = sum((i - mx) * (j - my) for i, j in zip(x, y))
        return cov / sqrt(vx * vy)

    def evaluate(
        self,
        *,
        net_liquidation: float,
        positions: tuple[ExposurePosition, ...],
        proposed_symbol: str,
        proposed_side: str,
        proposed_notional: float,
        proposed_sector: str = "UNKNOWN",
        proposed_returns: tuple[float, ...] = (),
    ) -> ExposureDecision:
        if net_liquidation <= 0 or proposed_notional <= 0:
            return ExposureDecision(False, "invalid_exposure_input", 0.0, 0.0, 0.0)

        same_sector = 0.0
        correlated = 0.0
        max_corr = 0.0
        side = proposed_side.upper()
        for position in positions:
            if position.side.upper() != side:
                continue
            if proposed_sector != "UNKNOWN" and position.sector == proposed_sector:
                same_sector += max(0.0, position.notional)
            corr = abs(self.correlation(position.returns, proposed_returns))
            max_corr = max(max_corr, corr)
            if corr >= self.correlation_threshold:
                correlated += max(0.0, position.notional)

        same_sector_pct = (same_sector + proposed_notional) / net_liquidation
        correlated_pct = (correlated + proposed_notional) / net_liquidation
        if same_sector_pct > self.max_same_sector_pct:
            return ExposureDecision(False, "same_sector_exposure_limit", correlated_pct, same_sector_pct, max_corr)
        if correlated_pct > self.max_correlated_cluster_pct:
            return ExposureDecision(False, "correlated_cluster_limit", correlated_pct, same_sector_pct, max_corr)
        return ExposureDecision(True, "cross_exposure_available", correlated_pct, same_sector_pct, max_corr)
