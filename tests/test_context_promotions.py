from pathlib import Path

from research.context_promotions import ContextPromotion, ContextPromotionStore


def test_latest_best_prefers_symbol_over_broader_context(tmp_path: Path) -> None:
    store = ContextPromotionStore(tmp_path / "test.db")
    store.record(ContextPromotion("timeframe", "1 hour", "mean_reversion_v1", "DEVELOP", 50, 10, 60, 30, 2, 5, 1.1))
    store.record(ContextPromotion("universe", "financials", "mean_reversion_v1", "DEVELOP", 61, 15, 100, 40, 5, 7, 1.15))
    store.record(ContextPromotion("symbol", "GS", "mean_reversion_v1", "PROMOTE", 76, 3, 100, 100, 9, 8, 1.24))

    row = store.latest_best(
        strategy="mean_reversion_v1",
        symbol="GS",
        universe="financials",
        timeframe="1 hour",
    )
    assert row is not None
    assert row["scope"] == "symbol"
    assert row["status"] == "PROMOTE"


def test_latest_returns_newest_record(tmp_path: Path) -> None:
    store = ContextPromotionStore(tmp_path / "test.db")
    first = ContextPromotion("symbol", "COIN", "momentum_gap_v1", "DEVELOP", 60, 3, 67, 50, 5, 10, 1.1)
    second = ContextPromotion("symbol", "COIN", "momentum_gap_v1", "PROMOTE", 81, 3, 100, 83, 13, 14, 1.12)
    store.record(first)
    store.record(second)
    row = store.latest(scope="symbol", context="COIN", strategy="momentum_gap_v1")
    assert row is not None
    assert row["status"] == "PROMOTE"
    assert row["score"] == 81
