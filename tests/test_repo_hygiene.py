"""Guards that keep private runtime data out of a PUBLIC repository.

These tests do not replace review; they catch the known classes of accident:
committing .env, state/, reports/, logs or SQLite databases, or pasting an
IBKR account identifier into tracked source.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or not (ROOT / ".git").exists(),
    reason="git repository not available",
)


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        ".env.local",
        "state/risk_state.json",
        "state/strategy_performance.db",
        "reports/backtests/ALL_RESULTS.csv",
        "reports/history_cache/SPY_intraday_1y.json",
        "logs/bot.log",
        "anything/research.db",
        "anything/research.sqlite",
    ],
)
def test_private_paths_are_ignored(path):
    # check-ignore exits 0 when the path is ignored; --no-index evaluates rules only.
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "-q", path], cwd=ROOT
    )
    assert result.returncode == 0, f"{path} is not ignored by .gitignore"


def test_env_example_stays_versionable():
    result = subprocess.run(["git", "check-ignore", "--no-index", "-q", ".env.example"], cwd=ROOT)
    assert result.returncode == 1


def test_no_private_files_are_tracked():
    tracked = _git("ls-files").splitlines()
    forbidden = [
        p for p in tracked
        if p == ".env"
        or p.startswith(("state/", "reports/"))
        or p.endswith((".db", ".sqlite", ".sqlite3", ".log"))
    ]
    assert forbidden == []


# IBKR account identifiers: individual (U + digits), paper (DU + digits), advisor/FA
# variants (F/DF/I/DI prefixes). Tests/docs may use obviously synthetic IDs with
# the reserved suffix pattern *000000* (e.g. DU0000001) which are allowed.
ACCOUNT_RE = re.compile(r"\b(?:DU|DF|DI|U|F|I)\d{6,9}\b")
SYNTHETIC_RE = re.compile(r"\b(?:DU|DF|DI|U|F|I)0{4,}\d{0,5}\b")


def test_tracked_text_contains_no_real_account_ids():
    offenders: list[str] = []
    for rel in _git("ls-files").splitlines():
        path = ROOT / rel
        if path.suffix.lower() not in {".py", ".md", ".txt", ".toml", ".example", ".json", ".yml", ".yaml", ".cfg", ".ini"} \
                and path.name != ".env.example":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for match in ACCOUNT_RE.finditer(text):
            if not SYNTHETIC_RE.fullmatch(match.group(0)):
                offenders.append(f"{rel}: {match.group(0)[:3]}***")
    assert offenders == []
