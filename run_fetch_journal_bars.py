"""Fetch hourly bars (read-only) for every symbol KNT's scanners marked eligible in the journal.

Builds the survivorship-free bar set that ``run_pipeline_backtest.py --universe-source journal``
and ``run_score_shadow.py`` need: names discovered point-in-time, including ones that later
stop trading (fetch them before IBKR drops them). Writes ``reports/pit_cache/{SYMBOL}_intraday_1y.json``
in the same layout as the history cache, MERGED with what is already stored (older bars are
kept). Read-only session with a separate clientId; no orders.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
from pathlib import Path


def journal_contracts(db: str) -> list[tuple[str, int]]:
    """Every (symbol, conId) pair ever marked eligible — a ticker reused by a different
    company (new conId) is fetched separately, never merged into the old one's history."""
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT DISTINCT symbol, con_id FROM discovery_funnel WHERE status='ranked_eligible' AND con_id > 0 "
            "ORDER BY symbol, con_id"
        ).fetchall()
    return [(str(s), int(c)) for s, c in rows]


def merge_bars(existing: list[dict], fresh: list[dict]) -> list[dict]:
    """Union by bar time; fresh bars win on overlap. Older history is never discarded (FWD3
    needs months of it and IBKR only serves a rolling window)."""
    by_time = {str(r["time"]): r for r in existing}
    by_time.update({str(r["time"]): r for r in fresh})
    return [by_time[k] for k in sorted(by_time)]


def cache_file(out: Path, symbol: str, con_id: int, contracts: list[tuple[str, int]]) -> Path:
    """``{SYMBOL}_intraday_1y.json`` when the symbol maps to one conId; ``{SYMBOL}.{conId}_...``
    for every conId of a reused ticker."""
    shared = sum(1 for s, _ in contracts if s == symbol) > 1
    return out / (f"{symbol}.{con_id}_intraday_1y.json" if shared else f"{symbol}_intraday_1y.json")


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
            path = cache_file(out, symbol, con_id, contracts)
            existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
            merged = merge_bars(existing, payload)
            path.write_text(json.dumps(merged), encoding="utf-8")
            print(f"{symbol}: {len(bars)} fetched, {len(merged)} stored")
            await asyncio.sleep(args.pacing_seconds)  # IBKR historical pacing
    finally:
        await connection.disconnect()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=None, help="default: <repo>/state/strategy_performance.db")
    ap.add_argument("--out-dir", default="reports/pit_cache")
    ap.add_argument("--duration", default="1 Y")
    ap.add_argument("--pacing-seconds", type=float, default=10.5)
    asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    main()
