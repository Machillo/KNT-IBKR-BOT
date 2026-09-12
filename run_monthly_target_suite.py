from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import json

from ib_async import Stock

from backtest.monthly_target import evaluate_monthly_target, robustness_rank, write_monthly_target_report
from backtest.universes import VALIDATION_UNIVERSES, universe_symbols
from config.config import config
from core.connection import IBKRConnection
from market.history import HistoricalDataService, PriceBar
from strategies.library import SINGLE_ASSET_STRATEGIES
from strategies.momentum import MomentumStrategy


@dataclass(frozen=True)
class TargetProfile:
    name: str
    duration: str
    bar_size: str


PROFILES = {
    "intraday_1y": TargetProfile("intraday_1y", "1 Y", "1 hour"),
    "swing_5y": TargetProfile("swing_5y", "5 Y", "4 hours"),
    "long_10y": TargetProfile("long_10y", "10 Y", "1 day"),
}


def strategies() -> list[object]:
    return [MomentumStrategy(), *[factory() for factory in SINGLE_ASSET_STRATEGIES]]


def _cache_path(cache_dir: Path, symbol: str, profile: TargetProfile) -> Path:
    safe = f"{symbol}_{profile.name}.json".replace("/", "_")
    return cache_dir / safe


def _serialize_time(value) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _save_bars(path: Path, bars: list[PriceBar]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [
        {
            "time": _serialize_time(b.time),
            "open": b.open,
            "high": b.high,
            "low": b.low,
            "close": b.close,
            "volume": b.volume,
        }
        for b in bars
    ]
    path.write_text(json.dumps(payload), encoding="utf-8")


def _load_bars(path: Path) -> list[PriceBar]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [
        PriceBar(
            time=item["time"],
            open=float(item["open"]),
            high=float(item["high"]),
            low=float(item["low"]),
            close=float(item["close"]),
            volume=float(item.get("volume", 0.0)),
        )
        for item in raw
    ]


async def run(
    symbols: list[str],
    profiles: list[TargetProfile],
    *,
    output_dir: str,
    cache_dir: str,
    refresh: bool,
    target_pct: float,
) -> None:
    connection = IBKRConnection(config.ibkr)
    out = Path(output_dir)
    cache = Path(cache_dir)
    out.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    all_rows = []
    historical_requests = 0
    local_backtests = 0

    try:
        ib = await connection.connect()
        history = HistoricalDataService(ib)
        for symbol in symbols:
            symbol = symbol.strip().upper()
            if not symbol:
                continue
            try:
                qualified = await ib.qualifyContractsAsync(Stock(symbol, "SMART", "USD"))
                if not qualified:
                    print(f"{symbol}: SKIPPED could_not_qualify")
                    continue
                contract = qualified[0]
            except Exception as exc:
                print(f"{symbol}: ERROR qualification {exc}")
                continue

            for profile in profiles:
                cached = _cache_path(cache, symbol, profile)
                try:
                    if cached.exists() and not refresh:
                        bars = _load_bars(cached)
                        source = "CACHE"
                    else:
                        bars = await history.bars(
                            contract,
                            duration=profile.duration,
                            bar_size=profile.bar_size,
                        )
                        historical_requests += 1
                        _save_bars(cached, bars)
                        source = "IBKR"
                except Exception as exc:
                    print(f"{symbol} {profile.name}: ERROR history {exc}")
                    continue

                if len(bars) < 180:
                    print(f"{symbol} {profile.name}: SKIPPED insufficient_history bars={len(bars)} source={source}")
                    continue

                rows = evaluate_monthly_target(
                    symbol=symbol,
                    profile=profile.name,
                    timeframe=profile.bar_size,
                    duration=profile.duration,
                    bars=bars,
                    strategies=strategies(),
                    target_pct=target_pct,
                )
                all_rows.extend(rows)
                local_backtests += len(rows) * 2  # full sample + explicit OOS rerun
                write_monthly_target_report(rows, out, f"{symbol}_{profile.name}_MONTHLY_TARGET")

                baseline = [row for row in rows if row.scenario == "baseline"]
                best = max(baseline, key=robustness_rank)
                print(
                    f"{symbol:6s} {profile.name:12s} source={source:5s} bars={len(bars):5d} "
                    f"best={best.strategy:24s} cmpd/mo={best.compounded_monthly_pct:7.2f}% "
                    f"median={best.median_monthly_pct:7.2f}% hit5={best.target_5pct_hit_rate_pct:5.1f}% "
                    f"dd={best.max_drawdown_pct:6.2f}% OOS/mo={best.oos_compounded_monthly_pct:7.2f}% "
                    f"candidate={best.candidate_5pct}"
                )

        if not all_rows:
            print("No monthly-target rows produced.")
            return

        ranked = sorted(all_rows, key=robustness_rank, reverse=True)
        write_monthly_target_report(ranked, out, "MONTHLY_TARGET_ALL")
        write_monthly_target_report(ranked[:100], out, "MONTHLY_TARGET_TOP100")
        candidates = [row for row in ranked if row.candidate_5pct]
        write_monthly_target_report(candidates, out, "MONTHLY_TARGET_5PCT_CANDIDATES")

        print("\n=== ROBUST MONTHLY TARGET TOP 20 ===")
        for index, row in enumerate(ranked[:20], start=1):
            print(
                f"{index:2d}. {row.symbol:6s} {row.profile:12s} {row.strategy:24s} {row.scenario:18s} "
                f"cmpd/mo={row.compounded_monthly_pct:7.2f}% median={row.median_monthly_pct:7.2f}% "
                f"positive={row.positive_month_rate_pct:5.1f}% hit5={row.target_5pct_hit_rate_pct:5.1f}% "
                f"DD={row.max_drawdown_pct:6.2f}% OOS/mo={row.oos_compounded_monthly_pct:7.2f}% "
                f"OOS_DD={row.oos_max_drawdown_pct:6.2f}% candidate={row.candidate_5pct}"
            )
        print(
            f"\nSUMMARY | rows={len(all_rows)} local_backtests~{local_backtests} "
            f"historical_requests={historical_requests} target={target_pct:.2f}%/month "
            f"qualified_candidates={len(candidates)}"
        )
        print(f"Reports: {out.resolve()}")
        print(f"History cache: {cache.resolve()}")
    finally:
        await connection.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Massive restartable KNT robustness suite for a monthly return target. "
                    "The target is a research threshold, never a return guarantee."
    )
    parser.add_argument("symbols", nargs="*", help="Additional arbitrary US stocks/ETFs")
    parser.add_argument(
        "--universe",
        choices=[*VALIDATION_UNIVERSES.keys(), "all"],
        default="all",
        help="Diagnostic research cohort only; does not become KNT's trading whitelist",
    )
    parser.add_argument(
        "--profile",
        choices=[*PROFILES.keys(), "all"],
        default="all",
        help="Historical horizon to test",
    )
    parser.add_argument("--target-monthly-pct", type=float, default=5.0)
    parser.add_argument("--output-dir", default="reports/monthly_target")
    parser.add_argument("--cache-dir", default="reports/history_cache")
    parser.add_argument("--refresh", action="store_true", help="Ignore cache and request history again")
    args = parser.parse_args()

    selected = list(args.symbols)
    if args.universe:
        selected.extend(universe_symbols(args.universe))
    selected = list(dict.fromkeys(symbol.upper() for symbol in selected if symbol.strip()))
    if not selected:
        parser.error("provide symbols and/or --universe")
    if args.target_monthly_pct <= 0:
        parser.error("--target-monthly-pct must be > 0")

    selected_profiles = list(PROFILES.values()) if args.profile == "all" else [PROFILES[args.profile]]
    asyncio.run(run(
        selected,
        selected_profiles,
        output_dir=args.output_dir,
        cache_dir=args.cache_dir,
        refresh=args.refresh,
        target_pct=args.target_monthly_pct,
    ))


if __name__ == "__main__":
    main()
