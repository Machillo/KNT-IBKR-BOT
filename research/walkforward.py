from __future__ import annotations

from dataclasses import dataclass

from backtest.engine import BacktestEngine
from market.history import PriceBar
from market.regime import RegimeDetector
from research.performance import StrategyPerformanceStore


@dataclass(frozen=True)
class WalkForwardSummary:
    strategy: str
    windows: int
    train_windows: int
    oos_windows: int


class WalkForwardResearch:
    """Rolling train/OOS evaluator used to generate learning evidence.

    Defaults deliberately leave enough bars in both TRAIN and OOS for strategies
    with 50-60 bar warmups. This keeps the Learning Engine from treating tiny,
    effectively untradeable test windows as meaningful evidence.
    """

    def __init__(self, store: StrategyPerformanceStore, engine: BacktestEngine | None = None) -> None:
        self.store = store
        self.engine = engine or BacktestEngine()
        self.regime_detector = RegimeDetector()

    def evaluate(
        self,
        *,
        symbol: str,
        asset_class: str,
        timeframe: str,
        bars: list[PriceBar],
        strategies: list[object],
        train_bars: int = 240,
        test_bars: int = 80,
        step_bars: int = 80,
    ) -> list[WalkForwardSummary]:
        if train_bars < 120 or test_bars < 60 or step_bars < 1:
            raise ValueError("Invalid walk-forward window sizes")
        summaries: list[WalkForwardSummary] = []
        for strategy in strategies:
            train_count = 0
            oos_count = 0
            start = 0
            while start + train_bars + test_bars <= len(bars):
                train = bars[start:start + train_bars]
                oos = bars[start + train_bars:start + train_bars + test_bars]
                train_regime = self.regime_detector.evaluate(train).regime.value
                oos_regime = self.regime_detector.evaluate(oos).regime.value
                train_result = self.engine.run(train, strategy)
                oos_result = self.engine.run(oos, strategy)
                self.store.record_result(
                    symbol=symbol, asset_class=asset_class, timeframe=timeframe,
                    regime=train_regime, strategy=strategy.name, split="TRAIN",
                    bars=len(train), result=train_result,
                )
                self.store.record_result(
                    symbol=symbol, asset_class=asset_class, timeframe=timeframe,
                    regime=oos_regime, strategy=strategy.name, split="OOS",
                    bars=len(oos), result=oos_result,
                )
                train_count += 1
                oos_count += 1
                start += step_bars
            summaries.append(WalkForwardSummary(strategy.name, train_count + oos_count, train_count, oos_count))
        return summaries
