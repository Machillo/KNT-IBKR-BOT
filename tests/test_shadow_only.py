from config.config import BotConfig, IBKRConfig, RiskConfig, RuntimeConfig
from run_shadow_only import shadow_only_config


def test_shadow_only_config_cannot_trade():
    base = BotConfig(
        ibkr=IBKRConfig(port=7497, client_id=901, readonly=False),
        risk=RiskConfig(kill_switch_dry_run=False),
        runtime=RuntimeConfig(autonomous_trading_enabled=True),
    )
    cfg = shadow_only_config(base)
    assert cfg.ibkr.readonly is True and cfg.ibkr.client_id == 954
    assert cfg.risk.kill_switch_dry_run is True
    assert cfg.runtime.autonomous_trading_enabled is False
    # Limits are untouched.
    assert cfg.risk.max_trade_risk_pct == base.risk.max_trade_risk_pct
    assert cfg.risk.max_daily_loss_pct == base.risk.max_daily_loss_pct


def test_shadow_only_runner_never_builds_an_executor():
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "run_shadow_only.py").read_text(encoding="utf-8")
    assert "PaperExecutionEngine" not in text and "OrderManager" not in text
    assert "paper_executor=None" in text


def _supervisor(state_dir, net_liq, **risk):
    import asyncio
    from types import SimpleNamespace

    from engine.supervisor import PaperSupervisor

    class FakeIB:
        client = SimpleNamespace(port=7497)

        def isConnected(self):
            return True

        def managedAccounts(self):
            return ["DU0000001"]

        async def accountSummaryAsync(self, account):
            return [SimpleNamespace(account="DU0000001", tag="NetLiquidation", currency="BASE", value=str(net_liq))]

        def positions(self):
            return []

        def openTrades(self):
            return []

    cfg = shadow_only_config(BotConfig(ibkr=IBKRConfig(port=7497, allow_live_trading=False),
                                       risk=RiskConfig(**risk), runtime=RuntimeConfig()))
    sup = PaperSupervisor(FakeIB(), cfg, state_dir=state_dir)
    asyncio.run(sup.initialize())
    return sup


def test_research_lock_sees_real_locks_despite_the_readonly_lock(isolated_state_dir):
    from run_shadow_only import research_locked

    sup = _supervisor(isolated_state_dir, 100_000)
    # The readonly session is never verified as paper: the real RiskManager is locked for that.
    assert sup.context.risk.trading_locked and "readonly_session" in sup.context.risk.lock_reason
    assert research_locked(sup, 100_000) is None                    # nothing else wrong
    assert research_locked(sup, 85_000) == "daily_loss_limit"       # -15 % today (limit 10 %)
    assert research_locked(sup, None) == "equity_unavailable"


def test_research_lock_sees_a_persisted_drawdown_lock(isolated_state_dir):
    from run_shadow_only import research_locked

    sup = _supervisor(isolated_state_dir, 100_000, max_drawdown_pct=0.05, max_daily_loss_pct=0.5)
    sup.drawdown.evaluate(94_000)                                   # -6 % from the high-water mark
    assert research_locked(sup, 94_000) == "multi_day_drawdown"


def test_shadow_only_refuses_live_settings():
    import pytest

    base = BotConfig(ibkr=IBKRConfig(port=7496, allow_live_trading=True, client_id=901),
                     risk=RiskConfig(), runtime=RuntimeConfig())
    cfg = shadow_only_config(base)
    assert cfg.ibkr.allow_live_trading is False
    with pytest.raises(RuntimeError):
        cfg.validate()  # the live port is refused before any connection
    with pytest.raises(RuntimeError):
        shadow_only_config(BotConfig(ibkr=IBKRConfig(port=7497, client_id=-53), risk=RiskConfig(),
                                     runtime=RuntimeConfig()))


def test_research_lock_mirrors_the_trading_bots_real_locks_read_only(isolated_state_dir):
    import json

    from run_shadow_only import research_locked

    sup = _supervisor(isolated_state_dir / "shadow_only", 100_000)
    real = isolated_state_dir
    assert research_locked(sup, 100_000, real_state_dir=real) is None       # bot never ran: no files
    assert not (real / "risk_state.json").exists()                           # and nothing was created
    record = {"account": sup.context.account, "trading_date": sup.context.trading_date,
              "starting_equity": 100000.0, "kill_switch_triggered": True,
              "trigger_reason": "Daily loss limit reached", "updated_at_utc": ""}
    (real / "risk_state.json").write_text(json.dumps(
        {"version": 1, "records": {f"{sup.context.account}:{sup.context.trading_date}": record}}), encoding="utf-8")
    before = (real / "risk_state.json").read_bytes()
    assert research_locked(sup, 100_000, real_state_dir=real) == "bot_sticky_daily_kill"
    assert (real / "risk_state.json").read_bytes() == before
    (real / "risk_state.json").unlink()
    (real / "drawdown_state.json").write_text("{corrupt", encoding="utf-8")
    assert research_locked(sup, 100_000, real_state_dir=real) == "bot_state_unreadable"


def test_shadow_only_refuses_to_restart_on_code_or_config_that_breaks_a_registered_window(tmp_path, monkeypatch):
    import pytest
    from research import fwd_protocol
    from run_shadow_only import decision_config_hash, fwd_window_guard

    cfg = shadow_only_config(BotConfig(ibkr=IBKRConfig(port=7497, client_id=901), risk=RiskConfig(),
                                       runtime=RuntimeConfig()))
    db = tmp_path / "strategy_performance.db"
    assert fwd_window_guard(db, cfg) is None                                  # nothing registered
    fwd_protocol.register_window(db, config_hash=decision_config_hash(cfg))
    assert fwd_window_guard(db, cfg) is None                                  # same code + config
    other = shadow_only_config(BotConfig(ibkr=IBKRConfig(port=7497, client_id=901),
                                         risk=RiskConfig(max_position_pct=0.05), runtime=RuntimeConfig()))
    assert "config differs" in fwd_window_guard(db, other)
    monkeypatch.setattr("research.shadow_journal.decision_fingerprint", lambda: "changed")
    assert "code differs" in fwd_window_guard(db, cfg)
    assert fwd_window_guard(db, cfg, end_window=True) is None                 # explicit acknowledgement


def test_window_guard_fails_closed_on_a_corrupt_registration_and_releases_an_ended_one(tmp_path):
    from research import fwd_protocol
    from run_shadow_only import decision_config_hash, fwd_window_guard

    cfg = shadow_only_config(BotConfig(ibkr=IBKRConfig(port=7497, client_id=901), risk=RiskConfig(),
                                       runtime=RuntimeConfig()))
    db = tmp_path / "strategy_performance.db"
    fwd_protocol.window_file(db).write_text("{corrupt", encoding="utf-8")
    assert "unreadable" in fwd_window_guard(db, cfg)
    fwd_protocol.window_file(db).unlink()
    fwd_protocol.register_window(db, config_hash=decision_config_hash(cfg))
    fwd_protocol.end_window(db, "test")
    other = shadow_only_config(BotConfig(ibkr=IBKRConfig(port=7497, client_id=901),
                                         risk=RiskConfig(max_position_pct=0.05), runtime=RuntimeConfig()))
    assert fwd_window_guard(db, other) is None                    # an ended window no longer pins anything
    import json
    assert "ended_at_utc" in json.loads(fwd_protocol.window_file(db).read_text(encoding="utf-8"))


def test_state_dirs_can_be_shared_across_checkouts_but_never_relative(monkeypatch):
    import pytest

    import config.config as cfg
    monkeypatch.setenv("KNT_STATE_DIR", "relative/state")
    with pytest.raises(RuntimeError):
        cfg._state_dir_from_env("KNT_STATE_DIR", cfg.STATE_DIR)
    from pathlib import Path
    absolute = str(Path.cwd().resolve() / "shared_state")
    monkeypatch.setenv("KNT_STATE_DIR", absolute)
    assert cfg._state_dir_from_env("KNT_STATE_DIR", cfg.STATE_DIR) == Path(absolute)


def test_registration_refuses_until_every_pre_registration_condition_holds(tmp_path, monkeypatch):
    import run_shadow_only
    from research import fwd_protocol
    from research.shadow_journal import ShadowJournal
    from run_shadow_only import registration_refusal

    cfg = shadow_only_config(BotConfig(ibkr=IBKRConfig(port=7497, client_id=901),
                                       risk=RiskConfig(max_trade_risk_pct=0.01), runtime=RuntimeConfig()))
    from dataclasses import replace
    from config.config import MarketDataConfig
    cfg = replace(cfg, market_data=MarketDataConfig(market_data_type=1))
    db = tmp_path / "state" / "strategy_performance.db"
    monkeypatch.delenv("KNT_STATE_DIR", raising=False)
    monkeypatch.delenv("KNT_BOT_STATE_DIR", raising=False)
    assert "risk-limits-reviewed" in registration_refusal(cfg, db, False)
    assert "KNT_STATE_DIR" in registration_refusal(cfg, db, True)
    monkeypatch.setenv("KNT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("KNT_BOT_STATE_DIR", str(tmp_path / "bot"))
    monkeypatch.setattr(run_shadow_only, "BOT_STATE_DIR", tmp_path / "bot")
    assert "does not exist" in registration_refusal(cfg, db, True)
    (tmp_path / "bot").mkdir()
    assert registration_refusal(cfg, db, True) is None
    # A journal that already ran this code is a pre-registration look: a fresh state dir is required.
    j = ShadowJournal(db)
    j.record_cycle("c", universe=None, scanners=[], rows_per_scanner=1, quote_budget=1, market_data_type=1,
                   session="REGULAR", market_open=True, run_mode="shadow_only")
    j.record_cycle_end("c", eligible=0, attempted=0, errors=0, max_candidates=12, run_mode="shadow_only",
                       learning_mode="frozen", scanner_rows={}, scanner_errors={}, config_hash="x")
    assert "FRESH" in registration_refusal(cfg, db, True)
    fwd_protocol.register_window(db, config_hash="x")
    assert "already registered" in registration_refusal(cfg, db, True)
