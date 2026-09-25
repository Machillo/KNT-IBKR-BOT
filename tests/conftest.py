"""Tests must never touch real runtime state (account baselines, locks, journals) or real logs.

Layers (all automatic):
1. every test runs with STATE_DIR -> a per-test temp dir (``state_path()`` resolves at call time);
2. every test runs with the CWD set to a temp dir (catches any stray relative path);
3. the file log handler is removed (tests never write logs/bot.log);
4. after EACH test the real state/ directory is compared with its snapshot; any change fails
   that test by name, and the session fails as a backstop.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REAL_STATE = ROOT / "state"


def _snapshot() -> dict[str, tuple[int, int]]:
    if not REAL_STATE.exists():
        return {}
    return {str(p.relative_to(REAL_STATE)): (p.stat().st_mtime_ns, p.stat().st_size)
            for p in REAL_STATE.rglob("*") if p.is_file()}


_BEFORE: dict[str, tuple[int, int]] = {}


def pytest_sessionstart(session):
    _BEFORE.update(_snapshot())
    for handler in list(logging.getLogger("knt_ibkr_bot").handlers):
        if isinstance(handler, logging.FileHandler):
            logging.getLogger("knt_ibkr_bot").removeHandler(handler)
            handler.close()


def pytest_sessionfinish(session, exitstatus):
    if _snapshot() != _BEFORE:
        session.exitstatus = 1
        print("\nERROR: tests modified the real state directory")


@pytest.fixture(autouse=True)
def isolated_state_dir(tmp_path, monkeypatch):
    """Point the repo-anchored STATE_DIR at a per-test temp dir and run from a temp CWD."""
    state = tmp_path / "state"
    monkeypatch.setattr("config.config.STATE_DIR", state)
    monkeypatch.setattr("engine.supervisor.STATE_DIR", state)
    monkeypatch.chdir(tmp_path)
    before = _snapshot()
    yield state
    after = _snapshot()
    assert after == before, f"test modified the REAL state directory: {sorted(set(after) ^ set(before))}"


@pytest.fixture(autouse=True)
def fixed_code_version(monkeypatch):
    """Journals written by tests carry a fixed, clean code version (the real one depends on the
    developer's working tree and would make quality verdicts non-reproducible in tests)."""
    monkeypatch.setattr("research.shadow_journal.code_version", lambda: "test-sha")
