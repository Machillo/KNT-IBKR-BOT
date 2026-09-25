"""Named pipeline configurations used in the research log (docs/experiments/LOG.md).

Every variant is a hypothesis with a single stated change versus ``live_default``.
Adding a variant is cheap; PROMOTING one requires validation evidence and a single
frozen holdout evaluation (see strategy-validation skill).
"""
from __future__ import annotations

from research.pipeline_backtest import PipelineConfig

LIVE = PipelineConfig()

VARIANTS: dict[str, PipelineConfig] = {
    "live_default": LIVE,
}
