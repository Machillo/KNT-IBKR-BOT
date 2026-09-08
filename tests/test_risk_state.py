import json

import pytest

from risk.state_store import DailyRiskStateStore


def test_same_account_and_day_restores_original_baseline(tmp_path):
    path = tmp_path / "risk_state.json"
    first = DailyRiskStateStore(path)
    state1, created1 = first.load_or_create(
        account="DU_TEST", trading_date="2026-08-28", starting_equity=1000
    )
    second = DailyRiskStateStore(path)
    state2, created2 = second.load_or_create(
        account="DU_TEST", trading_date="2026-08-28", starting_equity=950
    )
    assert created1 is True
    assert created2 is False
    assert state1.starting_equity == 1000
    assert state2.starting_equity == 1000


def test_new_day_gets_new_baseline(tmp_path):
    store = DailyRiskStateStore(tmp_path / "risk_state.json")
    store.load_or_create(account="DU_TEST", trading_date="2026-08-28", starting_equity=1000)
    state, created = store.load_or_create(
        account="DU_TEST", trading_date="2026-08-29", starting_equity=975
    )
    assert created is True
    assert state.starting_equity == 975


def test_accounts_are_isolated(tmp_path):
    store = DailyRiskStateStore(tmp_path / "risk_state.json")
    a, _ = store.load_or_create(account="DU_A", trading_date="2026-08-28", starting_equity=1000)
    b, _ = store.load_or_create(account="DU_B", trading_date="2026-08-28", starting_equity=2000)
    assert a.starting_equity == 1000
    assert b.starting_equity == 2000


def test_kill_switch_flag_is_sticky_across_restart(tmp_path):
    path = tmp_path / "risk_state.json"
    store = DailyRiskStateStore(path)
    store.load_or_create(account="DU_TEST", trading_date="2026-08-28", starting_equity=1000)
    store.mark_triggered(
        account="DU_TEST", trading_date="2026-08-28", reason="Daily loss limit reached"
    )
    restored, created = DailyRiskStateStore(path).load_or_create(
        account="DU_TEST", trading_date="2026-08-28", starting_equity=1100
    )
    assert created is False
    assert restored.kill_switch_triggered is True
    assert restored.trigger_reason == "Daily loss limit reached"
    assert restored.starting_equity == 1000


def test_corrupt_state_fails_closed(tmp_path):
    path = tmp_path / "risk_state.json"
    path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(RuntimeError, match="unreadable/corrupt"):
        DailyRiskStateStore(path).load_or_create(
            account="DU_TEST", trading_date="2026-08-28", starting_equity=1000
        )
