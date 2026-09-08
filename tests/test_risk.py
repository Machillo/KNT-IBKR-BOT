from config.config import RiskConfig
from risk.risk_manager import RiskManager


def manager(**overrides):
    base = dict(
        max_trade_risk_pct=0.10,
        max_daily_loss_pct=0.10,
        max_position_pct=1.00,
        kill_switch_enabled=True,
    )
    base.update(overrides)
    return RiskManager(RiskConfig(**base))


def test_trade_at_risk_limit_is_approved():
    risk = manager()
    decision = risk.evaluate_trade(
        equity=1000,
        entry_price=100,
        stop_price=90,
        quantity=10,
    )
    assert decision.approved
    assert decision.capital_at_risk == 100


def test_trade_over_risk_limit_is_rejected():
    risk = manager()
    decision = risk.evaluate_trade(
        equity=1000,
        entry_price=100,
        stop_price=80,
        quantity=10,
    )
    assert not decision.approved
    assert decision.capital_at_risk == 200


def test_position_cap_is_independent_from_stop_risk():
    risk = manager(max_position_pct=0.10)
    decision = risk.evaluate_trade(
        equity=1000,
        entry_price=100,
        stop_price=99,
        quantity=2,
    )
    assert not decision.approved
    assert "Position value" in decision.reason


def test_max_quantity_uses_tighter_constraint():
    risk = manager(max_position_pct=1.0)
    assert risk.max_quantity_for_risk(
        equity=1000,
        entry_price=100,
        stop_price=95,
    ) == 10


def test_daily_loss_threshold_triggers():
    risk = manager()
    state = risk.daily_state(starting_equity=1000, current_equity=900)
    assert state.kill_switch_required
    assert state.loss_pct == 0.10


def test_trading_lock_rejects_new_trade():
    risk = manager()
    risk.lock_trading("daily loss limit")
    decision = risk.evaluate_trade(
        equity=1000,
        entry_price=100,
        stop_price=95,
        quantity=1,
    )
    assert not decision.approved
    assert "Trading locked" in decision.reason
