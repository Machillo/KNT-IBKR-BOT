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
                 max_age: timedelta = timedelta(days=1)) -> None:
        self.max_age = max_age
        with sqlite3.connect(Path(db_path)) as conn:
            # LEFT JOIN: a cycle whose funnel is empty is a real (empty) snapshot and must
            # replace the previous membership instead of silently carrying it forward.
            rows = conn.execute(
                "SELECT c.created_at, f.symbol, f.status FROM discovery_cycles c "
                "LEFT JOIN discovery_funnel f ON f.cycle_id=c.cycle_id ORDER BY c.created_at"
            ).fetchall()
        cycles: dict[datetime, set[str]] = {}
        for created, symbol, status in rows:
            t = _naive_utc(created)
            if t is None:
                continue
            members = cycles.setdefault(t, set())
            if symbol is not None and status in statuses:
                members.add(symbol)
        self.times = sorted(cycles)
        self.members = [frozenset(cycles[t]) for t in self.times]

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


class PointInTimeCsvUniverse:
    """External survivorship-free membership file (see docs/POINT_IN_TIME_DATA.md).

    Convention: a snapshot dated D describes membership KNOWN AT THE CLOSE of D; it takes
    effect ``effective_lag_days`` later (default 1: from the next day) so same-day bars
    never see it. ``delisted_on`` is enforced against the query time t, and a snapshot
    older than ``max_age_days`` yields an empty universe (fail closed).
    """

    survivorship_biased = False

    def __init__(self, path: str | Path, *, effective_lag_days: int = 1, max_age_days: int = 45) -> None:
        self.lag = timedelta(days=max(0, int(effective_lag_days)))
        self.max_age = timedelta(days=max(1, int(max_age_days)))
        snapshots: dict[datetime, dict[str, datetime | None]] = {}
        with Path(path).open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                date = _naive_utc(row["date"])
                if date is None:
                    raise ValueError(f"bad date in point-in-time universe: {row['date']!r}")
                members = snapshots.setdefault(date + self.lag, {})
                if str(row.get("in_universe", "1")).strip() in {"1", "true", "True"}:
                    members[row["symbol"].strip().upper()] = _naive_utc(row.get("delisted_on") or "")
        self.times = sorted(snapshots)
        self.members = [snapshots[t] for t in self.times]

    def members_at(self, t) -> frozenset[str]:
        when = _naive_utc(t)
        i = -1 if when is None else bisect_right(self.times, when) - 1
        if i < 0 or when - self.times[i] > self.max_age:
            return frozenset()
        return frozenset(sym for sym, delisted in self.members[i].items() if delisted is None or delisted > when)
