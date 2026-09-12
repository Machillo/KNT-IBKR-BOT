from pathlib import Path
import sqlite3

from backtest.engine import BacktestResult
from research.performance import PerformanceEvidence, StrategyPerformanceStore


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


def test_store_persists_learning_assessment_history(tmp_path: Path):
    store = StrategyPerformanceStore(tmp_path / "perf.db")
    evidence = PerformanceEvidence(
        samples=4, trades=90, mean_return_pct=5.0, mean_drawdown_pct=4.0,
        mean_win_rate_pct=53.0, mean_profit_factor=1.3, mean_sharpe=0.8,
        oos_samples=2, evidence_score=8.0,
    )
    store.record_learning_assessment(
        symbol="NVDA", asset_class="STK", timeframe="1 hour", regime="TRENDING",
        strategy="momentum_gap_v1", status="TRUSTED", confidence=64.0,
        selector_bonus=7.0, freshness_factor=1.0,
        reason="positive_validated_evidence", evidence=evidence,
    )
    history = store.learning_history(
        symbol="NVDA", asset_class="STK", timeframe="1 hour", regime="TRENDING",
        strategy="momentum_gap_v1",
    )
    assert len(history) == 1
    assert history[0]["status"] == "TRUSTED"
    assert history[0]["confidence"] == 64.0
    assert history[0]["evidence_score"] == 8.0
    assert history[0]["trades"] == 90


def test_latest_completed_research_run_drives_evidence_without_deleting_history(tmp_path: Path):
    store = StrategyPerformanceStore(tmp_path / "perf.db")
    run1 = store.begin_research_run(
        symbol="SPY", asset_class="STK", timeframe="1 hour", bars=1000,
        dataset_start="2024-01-01", dataset_end="2024-06-01",
    )
    store.record_result(
        symbol="SPY", asset_class="STK", timeframe="1 hour", regime="TRENDING",
        strategy="momentum_v1", split="OOS", bars=80,
        result=_result(12.0, 4.0, 100, 1.8, 1.4), run_id=run1,
    )
    store.finish_research_run(run1)

    run2 = store.begin_research_run(
        symbol="SPY", asset_class="STK", timeframe="1 hour", bars=1200,
        dataset_start="2024-01-01", dataset_end="2024-09-01",
    )
    store.record_result(
        symbol="SPY", asset_class="STK", timeframe="1 hour", regime="TRENDING",
        strategy="momentum_v1", split="OOS", bars=80,
        result=_result(-5.0, 9.0, 20, 0.7, -0.5), run_id=run2,
    )
    store.finish_research_run(run2)

    evidence = store.evidence(
        symbol="SPY", asset_class="STK", timeframe="1 hour",
        regime="TRENDING", strategy="momentum_v1",
    )
    assert evidence is not None
    assert evidence.samples == 1
    assert evidence.trades == 20
    assert evidence.mean_return_pct == -5.0
    with sqlite3.connect(store.path) as conn:
        total_rows = conn.execute("SELECT COUNT(*) FROM strategy_performance").fetchone()[0]
    assert total_rows == 2


def test_existing_v1_database_is_migrated_in_place(tmp_path: Path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE strategy_performance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                symbol TEXT NOT NULL, asset_class TEXT NOT NULL, timeframe TEXT NOT NULL,
                regime TEXT NOT NULL, strategy TEXT NOT NULL, split TEXT NOT NULL,
                bars INTEGER NOT NULL, trades INTEGER NOT NULL,
                total_return_pct REAL NOT NULL, max_drawdown_pct REAL NOT NULL,
                win_rate_pct REAL NOT NULL, profit_factor REAL, sharpe REAL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO strategy_performance (
                symbol, asset_class, timeframe, regime, strategy, split, bars, trades,
                total_return_pct, max_drawdown_pct, win_rate_pct, profit_factor, sharpe
            ) VALUES ('SPY','STK','1 hour','TRENDING','momentum_v1','OOS',80,25,3,4,52,1.2,0.5)
            """
        )
    store = StrategyPerformanceStore(path)
    with sqlite3.connect(path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(strategy_performance)")}
        legacy_rows = conn.execute("SELECT COUNT(*) FROM strategy_performance").fetchone()[0]
        run_tables = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='research_runs'"
        ).fetchone()[0]
    assert {"run_id", "source", "strategy_version"}.issubset(columns)
    assert legacy_rows == 1
    assert run_tables == 1
    evidence = store.evidence(
        symbol="SPY", asset_class="STK", timeframe="1 hour",
        regime="TRENDING", strategy="momentum_v1",
    )
    assert evidence is not None
    assert evidence.trades == 25
