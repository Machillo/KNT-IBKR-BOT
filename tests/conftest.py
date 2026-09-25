"""Tests must never touch the real runtime state directory (account baselines, locks, journals)."""
from __future__ import annotations

from pathlib import Path

import pytest

REAL_STATE = Path(__file__).resolve().parents[1] / "state"


def _snapshot() -> dict[str, tuple[int, int]]:
    if not REAL_STATE.exists():
        return {}
    return {p.name: (p.stat().st_mtime_ns, p.stat().st_size) for p in REAL_STATE.iterdir() if p.is_file()}


_BEFORE: dict[str, tuple[int, int]] = {}


def pytest_sessionstart(session):
    _BEFORE.update(_snapshot())


def pytest_sessionfinish(session, exitstatus):
    after = _snapshot()
    if after != _BEFORE:
        changed = sorted(set(after) ^ set(_BEFORE) | {k for k in after if after.get(k) != _BEFORE.get(k)})
        session.exitstatus = 1
        print(f"\nERROR: tests modified the real state directory: {changed}")


@pytest.fixture(autouse=True)
def isolated_state_dir(tmp_path, monkeypatch):
    """Point the repo-anchored STATE_DIR at a per-test temporary directory."""
    state = tmp_path / "state"
    monkeypatch.setattr("config.config.STATE_DIR", state)
    monkeypatch.setattr("engine.supervisor.STATE_DIR", state)
    return state
