"""Read-only view of the trading bot's persisted locks (never creates or writes state)."""
from __future__ import annotations

from pathlib import Path

from risk.drawdown_guard import DrawdownStateStore
from risk.state_store import DailyRiskStateStore


def persisted_lock_reason(state_dir: Path, account: str, trading_date: str) -> str | None:
    """``bot_sticky_daily_kill`` / ``bot_multi_day_drawdown`` / ``bot_state_unreadable``, or None.

    Missing files mean the bot never ran there (no lock); unreadable or corrupt files fail
    closed.
    """
    state_dir = Path(state_dir)
    try:
        daily = DailyRiskStateStore(state_dir / "risk_state.json").get(account=account, trading_date=trading_date)
        drawdown = DrawdownStateStore(state_dir / "drawdown_state.json").get(account)
    except Exception:
        return "bot_state_unreadable"
    if daily is not None and daily.kill_switch_triggered:
        return "bot_sticky_daily_kill"
    if drawdown is not None and drawdown.locked:
        return "bot_multi_day_drawdown"
    return None


def drawdown_state_missing_with_history(state_dir: Path, account: str, trading_date: str) -> bool | None:
    """True when the account has daily-risk history but no drawdown record: the supervisor will
    refuse to initialize the high-water mark and lock entries on every poll (fail closed) until
    a human runs run_reset_drawdown_lock.py. None if the state is unreadable."""
    state_dir = Path(state_dir)
    try:
        history = DailyRiskStateStore(state_dir / "risk_state.json").has_prior_days(
            account=account, trading_date=trading_date)
        record = DrawdownStateStore(state_dir / "drawdown_state.json").get(account)
    except Exception:
        return None
    return bool(history and record is None)
