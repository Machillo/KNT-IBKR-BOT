"""Fetch hourly bars (read-only) for every symbol KNT's scanners marked eligible in the journal.

Builds the survivorship-free bar set that ``run_pipeline_backtest.py --universe-source journal``
and ``run_score_shadow.py`` need: names discovered point-in-time, including ones that later
stop trading (fetch them before IBKR drops them). Writes ``reports/pit_cache/{SYMBOL}_intraday_1y.json``
in the same layout as the history cache. Read-only session with a separate clientId; no orders.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
from pathlib import Path


def journal_contracts(db: str) -> list[tuple[str, int]]:
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT symbol, MAX(con_id) FROM discovery_funnel WHERE status='ranked_eligible' AND con_id > 0 "
            "GROUP BY symbol ORDER BY symbol"
        ).fetchall()
    return [(str(s), int(c)) for s, c in rows]


async def main_async(args) -> None:
    from ib_async import Contract

    from config.config import config, read_only_ibkr_settings
    from core.connection import IBKRConnection
    from market.history import HistoricalDataService

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    from config.config import state_path
    contracts = journal_contracts(args.db or str(state_path("strategy_performance.db")))
    connection = IBKRConnection(read_only_ibkr_settings(config.ibkr, 52))
    try:
        ib = await connection.connect()
        history = HistoricalDataService(ib)
        for symbol, con_id in contracts:
            try:
                bars = await history.bars(Contract(conId=con_id, exchange="SMART"), duration=args.duration,
                                          bar_size="1 hour", complete_only=True)
            except Exception as exc:
                print(f"{symbol}: ERROR {type(exc).__name__}")
                continue
            payload = [{"time": b.time.isoformat() if hasattr(b.time, "isoformat") else str(b.time),
                        "open": b.open, "high": b.high, "low": b.low, "close": b.close, "volume": b.volume}
                       for b in bars]
            (out / f"{symbol}_intraday_1y.json").write_text(json.dumps(payload), encoding="utf-8")
            print(f"{symbol}: {len(bars)} bars")
    finally:
        await connection.disconnect()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=None, help="default: <repo>/state/strategy_performance.db")
    ap.add_argument("--out-dir", default="reports/pit_cache")
    ap.add_argument("--duration", default="1 Y")
    asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    main()
