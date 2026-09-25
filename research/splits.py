"""Chronological splits and window evaluation without leakage.

Rules enforced here:
* splits are contiguous and ordered in time: TRAIN < VALIDATION < HOLDOUT;
* a window's backtest may READ earlier bars for indicator warmup, but only
  trades whose ENTRY bar lies inside the window count (engine ``trade_start``);
* nothing after the window's end is ever passed to the strategy.
"""
from __future__ import annotations

from dataclasses import dataclass

from backtest.engine import BacktestEngine, BacktestResult
from market.history import PriceBar

# Bars of history handed to strategies before a window starts. Strategies in
# this repo read at most ~80 bars; the regime detector's EMA200 needs ~400.
DEFAULT_CONTEXT_BARS = 450


@dataclass(frozen=True)
class Window:
    name: str
    start: int  # inclusive bar index
    end: int    # exclusive bar index

    def __post_init__(self) -> None:
        if not 0 <= self.start <= self.end:
            raise ValueError(f"invalid window {self}")

    @property
    def size(self) -> int:
        return self.end - self.start


@dataclass(frozen=True)
class ChronologicalSplit:
    train: Window
    validation: Window
    holdout: Window

    def windows(self) -> tuple[Window, Window, Window]:
        return self.train, self.validation, self.holdout


def chronological_split(n_bars: int, train: float = 0.6, validation: float = 0.2) -> ChronologicalSplit:
    if n_bars < 3:
        raise ValueError("need at least 3 bars")
    if not (0 < train < 1 and 0 < validation < 1 and train + validation < 1):
        raise ValueError("invalid split fractions")
    train_end = int(n_bars * train)
    validation_end = int(n_bars * (train + validation))
    return ChronologicalSplit(
        Window("TRAIN", 0, train_end),
        Window("VALIDATION", train_end, validation_end),
        Window("HOLDOUT", validation_end, n_bars),
    )


def run_window(engine: BacktestEngine, bars: list[PriceBar], strategy, window: Window,
               context_bars: int = DEFAULT_CONTEXT_BARS) -> BacktestResult:
    """Backtest ``strategy`` counting only entries inside ``window``.

    The slice passed to the engine ends at ``window.end`` (no future data) and
    starts ``context_bars`` earlier so indicators are warm at ``window.start``.
    Positions still open at ``window.end`` are closed at that window's last bar.
    """
    lo = max(0, window.start - max(0, int(context_bars)))
    return engine.run(bars[lo:window.end], strategy, trade_start=window.start - lo)


def rolling_windows(n_bars: int, *, train: int, test: int, step: int) -> list[tuple[Window, Window]]:
    """Walk-forward (train, test) pairs. ``step >= test`` is required so OOS windows never
    overlap: overlapping windows would count the same OOS trades several times."""
    if train < 1 or test < 1 or step < 1:
        raise ValueError("invalid rolling window sizes")
    if step < test:
        raise ValueError("step must be >= test so OOS windows do not overlap")
    pairs = []
    start = 0
    while start + train + test <= n_bars:
        pairs.append((Window("TRAIN", start, start + train),
                      Window("OOS", start + train, start + train + test)))
        start += step
    return pairs
