"""Named pipeline configurations used in the research log (docs/experiments/LOG.md).

Every variant is a pre-registered hypothesis with a single stated change versus
``live_default``. Adding a variant is cheap; PROMOTING one requires validation
evidence and a single frozen holdout evaluation (see strategy-validation skill).
"""
from __future__ import annotations

from research.pipeline_backtest import PipelineConfig

LIVE = PipelineConfig()

VARIANTS: dict[str, PipelineConfig] = {
    "live_default": LIVE,
    # H1: skip the MIXED regime.
    "h1_no_mixed": LIVE.variant(name="h1_no_mixed", regime_filter=("TRENDING", "RANGE")),
    # H2: higher conviction threshold.
    "h2_threshold_70": LIVE.variant(name="h2_threshold_70", min_score=70.0),
    # H3: trend core in trending regimes only.
    "h3_trend_core": LIVE.variant(
        name="h3_trend_core",
        strategies=("trend_following_v1", "momentum_gap_v1", "breakout_v1"),
        regime_filter=("TRENDING",),
    ),
    # H4: mean-reversion core in ranges only.
    "h4_reversion_core": LIVE.variant(
        name="h4_reversion_core",
        strategies=("mean_reversion_v1", "range_v1", "smc_liquidity_v1"),
        regime_filter=("RANGE",),
    ),
    # H5: research reference only (shorts are not executable today).
    "h5_with_shorts": LIVE.variant(name="h5_with_shorts", allow_short=True),
    # H6: no volatility down-sizing.
    "h6_no_vol_mult": LIVE.variant(name="h6_no_vol_mult", use_volatility_multiplier=False),
}
