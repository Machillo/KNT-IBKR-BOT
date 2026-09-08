from unittest.mock import Mock
import pytest

from config.config import RiskConfig
from core.exceptions import RiskRejectedError
from risk.order_gate import RiskGatedOrderManager
from risk.risk_manager import RiskManager


def make_gate():
    orders = Mock()
    risk = RiskManager(RiskConfig(
        max_trade_risk_pct=0.10,
        max_daily_loss_pct=0.10,
        max_position_pct=0.10,
        kill_switch_enabled=True,
    ))
    return RiskGatedOrderManager(orders, risk), orders


def contract(symbol="TEST"):
    c = Mock()
    c.localSymbol = symbol
    c.symbol = symbol
    return c


def test_approved_limit_reaches_order_manager():
    gate, orders = make_gate()
    orders.limit.return_value = "trade"
    result = gate.limit_entry(contract(), "BUY", 1, 100, equity=1000, stop_price=95)
    assert result == "trade"
    orders.limit.assert_called_once()


def test_rejected_limit_never_reaches_ibkr_layer():
    gate, orders = make_gate()
    with pytest.raises(RiskRejectedError):
        gate.limit_entry(contract(), "BUY", 2, 100, equity=1000, stop_price=80)
    orders.limit.assert_not_called()


def test_locked_risk_manager_blocks_new_market_entry():
    gate, orders = make_gate()
    gate.risk.lock_trading("kill switch")
    with pytest.raises(RiskRejectedError):
        gate.market_entry(
            contract(), "BUY", 1, equity=1000,
            reference_price=100, stop_price=95,
        )
    orders.market.assert_not_called()


def test_close_market_has_explicit_risk_reducing_path():
    gate, orders = make_gate()
    orders.market.return_value = "exit-trade"
    assert gate.close_market(contract(), "SELL", 1) == "exit-trade"
    orders.market.assert_called_once()
