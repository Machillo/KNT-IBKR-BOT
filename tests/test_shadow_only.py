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
