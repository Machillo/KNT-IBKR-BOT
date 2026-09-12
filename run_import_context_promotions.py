from __future__ import annotations

import argparse

from research.context_promotions import import_validation_promotions


def main() -> None:
    parser = argparse.ArgumentParser(description="Import contextual validation promotions into KNT research memory")
    parser.add_argument("--input", default="reports/backtests/ALL_RESULTS.csv")
    parser.add_argument("--db", default="state/strategy_performance.db")
    args = parser.parse_args()
    count = import_validation_promotions(args.input, db_path=args.db)
    print(f"Imported contextual promotion rows: {count}")


if __name__ == "__main__":
    main()
