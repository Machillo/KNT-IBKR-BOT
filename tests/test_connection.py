import pytest

from config.config import IBKRConfig


def test_live_tws_port_is_blocked_by_default():
    settings = IBKRConfig(port=7496, allow_live_trading=False)
    with pytest.raises(RuntimeError):
        settings.validate()


def test_paper_gateway_port_is_allowed():
    settings = IBKRConfig(port=4002, allow_live_trading=False)
    settings.validate()
