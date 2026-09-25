import subprocess
import sys
from pathlib import Path

from research.protocol import HOLDOUT_START, SEGMENTS, VALIDATION_START

ROOT = Path(__file__).resolve().parents[1]


def test_calendar_segments_are_ordered_and_contiguous():
    assert VALIDATION_START < HOLDOUT_START
    assert SEGMENTS["train"] == (None, VALIDATION_START)
    assert SEGMENTS["validation"] == (VALIDATION_START, HOLDOUT_START)
    assert SEGMENTS["development"] == (None, HOLDOUT_START)
    assert SEGMENTS["holdout"] == (HOLDOUT_START, None)


def _run(*args):
    return subprocess.run([sys.executable, *args], cwd=ROOT, capture_output=True, text=True)


def test_runners_refuse_holdout_without_explicit_confirmation():
    r1 = _run("run_pipeline_backtest.py", "--segment", "holdout")
    r2 = _run("run_experiments.py", "--segment", "holdout")
    r3 = _run("run_experiments.py", "--segment", "holdout", "--confirm-holdout")  # needs exactly one variant
    for r in (r1, r2, r3):
        assert r.returncode != 0 and "HOLDOUT" in r.stderr
