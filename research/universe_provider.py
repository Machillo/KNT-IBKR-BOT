"""Point-in-time universe providers for replay/research.

A replay may only decide on symbols that were KNOWN to be in the tradable universe at
decision time t. Providers answer ``members_at(t)`` and declare whether they carry
survivorship bias, so reports can state it.

* ``StaticCohortUniverse`` — a fixed list (e.g. the 38-name cache cohort). Always
  flagged ``survivorship_biased=True``: the list was chosen with hindsight.
* ``JournalUniverse`` — reconstructs the universe from KNT's own shadow journal
  (``discovery_funnel``): the names the IBKR scanners actually returned and the
  liquidity ranker marked eligible in the latest cycle at or before t. No hindsight,
  but it only exists from the day shadow mode started recording.
* ``PointInTimeCsvUniverse`` — for an external survivorship-free source (see
  docs/POINT_IN_TIME_DATA.md). Rows: ``date,symbol,identifier,in_universe`` with
  optional ``delisted_on``; membership as of the latest snapshot date <= t.
"""
from __future__ import annotations

import csv
import sqlite3
from bisect import bisect_right
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from backtest.metrics import as_datetime


def _naive_utc(value) -> datetime | None:
    dt = as_datetime(value)
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


class StaticCohortUniverse:
    survivorship_biased = True

    def __init__(self, symbols) -> None:
        self.symbols = frozenset(symbols)

    def members_at(self, t) -> frozenset[str]:
        return self.symbols


class JournalUniverse:
    """Membership = eligible names of the latest discovery cycle at or before t (max age)."""

    survivorship_biased = False

    def __init__(self, db_path: str | Path, *, statuses: tuple[str, ...] = ("ranked_eligible",),
                 max_age: timedelta = timedelta(days=1), top_n: int | None = None) -> None:
        """``top_n`` = the runtime's deep-analysis cap (SHADOW_MAX_CANDIDATES): only the top_n
        eligible names by liquidity score (ties: best scanner rank) are members, exactly the
        names the runtime analysed in that cycle. None keeps every eligible name."""
        self.max_age = max_age
        self.top_n = top_n
        with sqlite3.connect(Path(db_path)) as conn:
            # LEFT JOIN: a cycle whose funnel is empty is a real (empty) snapshot and must
            # replace the previous membership instead of silently carrying it forward.
            rows = conn.execute(
                "SELECT c.created_at, f.symbol, f.status, f.liquidity_score, f.best_rank FROM discovery_cycles c "
                "LEFT JOIN discovery_funnel f ON f.cycle_id=c.cycle_id ORDER BY c.created_at"
            ).fetchall()
        cycles: dict[datetime, dict[str, tuple[float, float]]] = {}
        for created, symbol, status, score, rank in rows:
            t = _naive_utc(created)
            if t is None:
                continue
            members = cycles.setdefault(t, {})
            if symbol is not None and status in statuses:
                key = (-(float(score) if score is not None else float("-inf")),
                       float(rank) if rank is not None else float("inf"))
                members[symbol] = min(members.get(symbol, key), key)
        self.times = sorted(cycles)
        self.members = []
        for t in self.times:
            ordered = sorted(cycles[t], key=lambda sym: (cycles[t][sym], sym))
            if top_n is not None:
                ordered = ordered[:max(0, int(top_n))]
            self.members.append(frozenset(ordered))

    def members_at(self, t) -> frozenset[str]:
        dt = as_datetime(t)
        if dt is not None and dt.tzinfo is None:
            # Replay times are exchange wall-clock (America/New_York) without tzinfo.
            dt = dt.replace(tzinfo=ZoneInfo("America/New_York"))
        when = _naive_utc(dt)
        if when is None:
            return frozenset()
        i = bisect_right(self.times, when) - 1
        if i < 0 or when - self.times[i] > self.max_age:
            return frozenset()  # fail closed: no recent snapshot, nobody is tradable
        return self.members[i]


_EXCHANGE_TZ = ZoneInfo("America/New_York")
REQUIRED_MEMBERSHIP_COLUMNS = ("date", "symbol", "identifier", "in_universe")
REQUIRED_SECTOR_COLUMNS = ("date", "symbol", "identifier", "sector")


def _exchange_naive(value) -> datetime | None:
    """Exchange (America/New_York) wall-clock time without tzinfo — the replay's time base.
    Aware values are converted; naive values are taken as exchange time already."""
    dt = as_datetime(value)
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(_EXCHANGE_TZ).replace(tzinfo=None)
    return dt


def _read_rows(path: str | Path, required: tuple[str, ...]) -> list[dict]:
    with Path(path).open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        missing = [c for c in required if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"point-in-time file {path} lacks columns {missing}")
        rows = list(reader)
    for n, row in enumerate(rows, 2):
        for column in required:
            if not str(row.get(column) or "").strip():
                raise ValueError(f"{path}:{n}: empty {column!r}")
    return rows


class PointInTimeCsvUniverse:
    """External survivorship-free membership file (spec: docs/POINT_IN_TIME_DATA.md).

    Convention: a snapshot dated D (exchange date) describes membership KNOWN AT THE CLOSE of
    D; it takes effect ``effective_lag_days`` later (default 1: from the next day) so same-day
    bars never see it. ``delisted_on`` is enforced against the query time t, and a snapshot
    older than ``max_age_days`` yields an empty universe (fail closed).

    Fails closed at LOAD time (ValueError) on: missing columns or values, one symbol mapped to
    two identifiers in the same snapshot (ambiguous ticker), and an identifier marked in the
    universe on or after its own delisting date (look-ahead or a data error). Ticker changes
    are fine: every snapshot carries the symbol valid on that date for each identifier.
    Times are exchange wall-clock (aware inputs are converted), like the replay's keys.
    """

    survivorship_biased = False

    def __init__(self, path: str | Path, *, effective_lag_days: int = 1, max_age_days: int = 45) -> None:
        self.lag = timedelta(days=max(0, int(effective_lag_days)))
        self.max_age = timedelta(days=max(1, int(max_age_days)))
        snapshots: dict[datetime, dict[str, datetime | None]] = {}
        owners: dict[tuple[datetime, str], str] = {}
        for row in _read_rows(path, REQUIRED_MEMBERSHIP_COLUMNS):
            date = _exchange_naive(row["date"])
            if date is None:
                raise ValueError(f"bad date in point-in-time universe: {row['date']!r}")
            symbol, identifier = row["symbol"].strip().upper(), row["identifier"].strip()
            delisted = _exchange_naive(row.get("delisted_on") or "")
            in_universe = str(row["in_universe"]).strip() in {"1", "true", "True"}
            if in_universe and delisted is not None and delisted <= date:
                raise ValueError(f"{identifier} ({symbol}) in universe on {date.date()} after delisting {delisted.date()}")
            owner = owners.setdefault((date, symbol), identifier)
            if owner != identifier:
                raise ValueError(f"ambiguous ticker {symbol} on {date.date()}: identifiers {owner} and {identifier}")
            members = snapshots.setdefault(date + self.lag, {})
            if in_universe:
                members[symbol] = delisted
        self.times = sorted(snapshots)
        self.members = [snapshots[t] for t in self.times]

    def members_at(self, t) -> frozenset[str]:
        when = _exchange_naive(t)
        i = -1 if when is None else bisect_right(self.times, when) - 1
        if i < 0 or when - self.times[i] > self.max_age:
            return frozenset()
        return frozenset(sym for sym, delisted in self.members[i].items() if delisted is None or delisted > when)


class PointInTimeSectors:
    """Point-in-time sector classification (``date,symbol,identifier,sector``), same lag and
    staleness convention as the membership file. ``sector_at(symbol, t)`` returns None when
    unknown or stale, so the admission coordinator's ``require_sector_metadata`` fails closed
    exactly as it does at runtime without IBKR contract details."""

    def __init__(self, path: str | Path, *, effective_lag_days: int = 1, max_age_days: int = 400) -> None:
        lag = timedelta(days=max(0, int(effective_lag_days)))
        self.max_age = timedelta(days=max(1, int(max_age_days)))
        history: dict[str, list[tuple[datetime, str]]] = {}
        for row in _read_rows(path, REQUIRED_SECTOR_COLUMNS):
            date = _exchange_naive(row["date"])
            if date is None:
                raise ValueError(f"bad date in sector file: {row['date']!r}")
            history.setdefault(row["symbol"].strip().upper(), []).append((date + lag, row["sector"].strip()))
        self.history = {sym: sorted(items) for sym, items in history.items()}

    def sector_at(self, symbol: str, t) -> str | None:
        when = _exchange_naive(t)
        items = self.history.get(str(symbol).upper(), [])
        i = -1 if when is None else bisect_right([d for d, _ in items], when) - 1
        if i < 0 or when - items[i][0] > self.max_age:
            return None
        return items[i][1]
