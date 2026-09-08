from ib_async import LimitOrder, MarketOrder


def test_ib_async_order_objects_can_be_created():
    market = MarketOrder("BUY", 1)
    limit = LimitOrder("SELL", 1, 100.0)
    assert market.orderType == "MKT"
    assert limit.orderType == "LMT"
