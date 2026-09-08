from pathlib import Path

from backtest.engine import BacktestResult
from research.performance import StrategyPerformanceStore


def _result(ret: float, dd: float, trades: int, pf: float, sharpe: float) -> BacktestResult:
    return BacktestResult(
        initial_equity=10000,
        final_equity=10000 * (1 + ret / 100),
        total_return_pct=ret,
        max_drawdown_pct=dd,
        trades=trades,
        wins=max(1, trades // 2),
        losses=max(0, trades - max(1, trades // 2)),
        win_rate_pct=50.0,
        profit_factor=pf,
        sharpe=sharpe,
        trade_log=(),
    )


def test_store_records_and_builds_oos_evidence(tmp_path: Path):
    store = StrategyPerformanceStore(tmp_path / "perf.db")
    store.record_result(
        symbol="SPY", asset_class="STK", timeframe="1 hour", regime="TRENDING",
        strategy="momentum_v1", split="TRAIN", bars=120,
        result=_result(8.0, 5.0, 50, 1.4, 1.0),
    )
    store.record_result(
        symbol="SPY", asset_class="STK", timeframe="1 hour", regime="TRENDING",
        strategy="momentum_v1", split="OOS", bars=40,
        result=_result(3.0, 3.0, 30, 1.2, 0.6),
    )
    evidence = store.evidence(
        symbol="SPY", asset_class="STK", timeframe="1 hour",
        regime="TRENDING", strategy="momentum_v1",
    )
    assert evidence is not None
    assert evidence.samples == 2
    assert evidence.trades == 80
    assert evidence.oos_samples == 1
    assert evidence.evidence_score > 0
