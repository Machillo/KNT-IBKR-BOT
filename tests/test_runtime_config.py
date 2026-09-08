import pytest

from config.config import RuntimeConfig


def test_safe_runtime_defaults_can_validate():
    RuntimeConfig().validate()


def test_supervisor_poll_must_not_be_subsecond():
    with pytest.raises(ValueError):
        RuntimeConfig(supervisor_poll_seconds=0.5).validate()


def test_discovery_rows_are_bounded():
    with pytest.raises(ValueError):
        RuntimeConfig(discovery_rows=100).validate()
