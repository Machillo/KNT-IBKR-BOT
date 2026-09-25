import csv

import pytest

from research.confluence_gen4 import admit_candidates
from run_confluence_gen4 import load_validation_candidates

FIELDS = ["symbol", "profile", "strategy", "test_monthly_pct", "test_dd_pct",
          "test_positive_month_rate_pct", "test_trades", "validation_monthly_pct",
          "validation_dd_pct", "validation_positive_month_rate_pct", "validation_trades"]


def test_gen4_admission_reads_validation_not_test_columns(tmp_path):
    path = tmp_path / "gen3.csv"
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerow(dict(symbol="X", profile="p", strategy="s", test_monthly_pct=9.9, test_dd_pct=0.1,
                        test_positive_month_rate_pct=100, test_trades=99, validation_monthly_pct=-0.5,
                        validation_dd_pct=3.0, validation_positive_month_rate_pct=40, validation_trades=15))
    rows = load_validation_candidates(path)
    assert rows[0].test_monthly_pct == -0.5 and rows[0].trades == 15
    assert admit_candidates(rows) == []  # excellent TEST numbers cannot buy admission


def test_gen4_refuses_legacy_reports_without_validation_columns(tmp_path):
    legacy = tmp_path / "legacy.csv"
    legacy.write_text("symbol,profile,strategy,test_monthly_pct,test_dd_pct,test_positive_month_rate_pct,test_trades\n")
    with pytest.raises(ValueError):
        load_validation_candidates(legacy)
