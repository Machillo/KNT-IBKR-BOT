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
