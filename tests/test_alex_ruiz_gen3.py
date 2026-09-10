from datetime import datetime, timedelta

from market.history import PriceBar
from research.alex_ruiz_gen3 import split_train_validation_test


def _bars(n: int):
    start = datetime(2020, 1, 1)
    return [PriceBar(time=start + timedelta(days=i), open=100+i, high=101+i, low=99+i, close=100+i, volume=1000) for i in range(n)]


def test_gen3_split_is_chronological_and_non_overlapping():
    bars = _bars(1000)
    train, validation, test = split_train_validation_test(bars)
    assert (len(train), len(validation), len(test)) == (600, 200, 200)
    assert train[-1].time < validation[0].time
    assert validation[-1].time < test[0].time
    assert train + validation + test == bars


def test_gen3_rejects_too_short_dataset():
    assert split_train_validation_test(_bars(299)) == ([], [], [])
