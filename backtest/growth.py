from __future__ import annotations

from dataclasses import dataclass
from random import Random


@dataclass(frozen=True)
class GrowthProjection:
    paths: int
    periods: int
    starting_equity: float
    median_final_equity: float
    p10_final_equity: float
    p90_final_equity: float
    probability_of_loss_pct: float
    probability_of_ruin_pct: float


def simulate_growth(
    period_returns: list[float],
    *,
    starting_equity: float = 10_000.0,
    periods: int = 252,
    paths: int = 5_000,
    ruin_fraction: float = 0.50,
    seed: int = 42,
) -> GrowthProjection:
    """Bootstrap observed net returns into account-growth distributions.

    `period_returns` are decimal net returns after costs (e.g. 0.01 == +1%).
    This is a risk projection, not a forecast or profit guarantee.
    """
    if starting_equity <= 0 or periods < 1 or paths < 100:
        raise ValueError("Invalid growth simulation parameters")
    if not period_returns:
        raise ValueError("period_returns cannot be empty")

    rng = Random(seed)
    finals: list[float] = []
    loss_paths = ruin_paths = 0
    ruin_level = starting_equity * max(0.0, min(1.0, ruin_fraction))

    for _ in range(paths):
        equity = starting_equity
        ruined = False
        for _ in range(periods):
            r = float(period_returns[rng.randrange(len(period_returns))])
            # A strategy cannot lose more than all current capital in one modeled period.
            equity *= max(0.0, 1.0 + max(-1.0, r))
            if equity <= ruin_level:
                ruined = True
        finals.append(equity)
        loss_paths += equity < starting_equity
        ruin_paths += ruined

    finals.sort()
    def pct(p: float) -> float:
        index = min(len(finals) - 1, max(0, int(round((len(finals) - 1) * p))))
        return finals[index]

    return GrowthProjection(
        paths=paths,
        periods=periods,
        starting_equity=starting_equity,
        median_final_equity=pct(0.50),
        p10_final_equity=pct(0.10),
        p90_final_equity=pct(0.90),
        probability_of_loss_pct=100.0 * loss_paths / paths,
        probability_of_ruin_pct=100.0 * ruin_paths / paths,
    )
