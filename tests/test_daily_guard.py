from config.config import RiskConfig
from risk.daily_guard import DailyLossGuard
from risk.risk_manager import RiskManager


def make_guard(starting=1000):
    risk = RiskManager(RiskConfig(
        max_trade_risk_pct=0.10,
        max_daily_loss_pct=0.10,
        max_position_pct=0.10,
        kill_switch_enabled=True,
        kill_switch_dry_run=True,
    ))
    return DailyLossGuard(risk, starting)


def test_below_threshold_does_not_lock():
    guard = make_guard()
    result = guard.evaluate(901)
    assert not result.action_required
    assert not result.trading_locked


def test_exact_threshold_locks_new_entries():
    guard = make_guard()
    result = guard.evaluate(900)
    assert result.action_required
    assert result.trading_locked


def test_above_threshold_remains_locked():
    guard = make_guard()
    guard.evaluate(899)
    result = guard.evaluate(950)
    assert result.trading_locked
