"""Score journaled shadow decisions once enough FUTURE bars exist.

Guarantees:
* Decisions are never modified: outcomes go to ``shadow_outcomes`` only.
* Future data is used only AFTER the decision, to evaluate it; the scorer reads
  bars strictly later than the decision's ``bar_time``.
* Idempotent and resumable: FINAL outcomes for the current ``SCORER_VERSION`` are
  skipped; PENDING_DATA outcomes are re-evaluated when more bars arrive.
* No broker order code; bars come from a pluggable provider (in-memory for tests,
  the offline cache, or read-only IBKR history).

Evaluated per decision:
* SELECTED — the signal the selector actually chose (TRADE-type actions);
* COUNTERFACTUAL — for NO_TRADE, the best directional evaluation that was below the
  threshold or filtered, if one existed ("what did we pass on?");
* forward close-to-close returns at 1 / 5 / 20 bars for every decision (TRADE or not),
  so NO_TRADE can be studied even when no signal existed.
"""
from __future__ import annotations

from config.config import state_path

import inspect
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from backtest.costs import BASELINE, CostModel
from backtest.metrics import as_datetime
from market.history import PriceBar
from research.shadow_journal import ShadowJournal, score_decision

# v2: bracket fills only for EXECUTABLE decisions (would really have been transmitted),
# unalignable rows expire to NOT_EVALUABLE, provider recorded, leave-one-out cycle benchmark.
# v3: forward returns start at the OPEN of the first bar that starts at/after the decision
#     (the first tradeable price; never the decision bar's close, never an overnight gap
#     before the decision existed), exactly h bars from there; DAY limit entries live only in
#     the session of ``created_at``.
SCORER_VERSION = "v3"
FORWARD_HORIZONS = (1, 5, 20)
MAX_BRACKET_BARS = 60
UNALIGNED_EXPIRY_DAYS = 14          # ~10 trading days
MIN_CYCLE_ROWS = 5                  # FWD protocol: cycles with fewer scored rows are excluded
EXECUTABLE_ACTIONS = ("PAPER_SUBMITTED", "SHADOW_SUBMIT")


def is_executable(action: str, market_open) -> bool:
    """Would this decision really have been transmitted?

    v2 rows say so explicitly (PAPER_SUBMITTED / SHADOW_SUBMIT). Legacy v1 rows only had
    APPROVED_*; they count only if the cycle's session was open (a decision on the session's
    last bar, taken after the close, is refused by the executor and must not be credited).
    """
    action = str(action or "")
    if action in EXECUTABLE_ACTIONS:
        return True
    return action.startswith("APPROVED_") and market_open is not None and int(market_open) == 1


def _naive(value) -> datetime | None:
    dt = as_datetime(value)
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


class InMemoryBarsProvider:
    """{symbol: [PriceBar,...]} — for tests and offline replays."""

    def __init__(self, data: dict[str, list[PriceBar]]) -> None:
        self.data = data

    def bars_from(self, symbol: str, con_id: int, at, timeframe: str) -> list[PriceBar]:
        """Bars at or after the decision bar (the decision bar itself must be included)."""
        cutoff = _naive(at)
        return [b for b in self.data.get(symbol, []) if cutoff is not None and _naive(b.time) >= cutoff]


class CacheBarsProvider(InMemoryBarsProvider):
    """Reads ``reports/history_cache/{SYMBOL}_{profile}.json`` for the decision timeframe."""

    PROFILE_BY_TIMEFRAME = {"1 hour": "intraday_1y", "4 hours": "swing_5y", "1 day": "long_10y"}

    def __init__(self, cache_dir: str | Path | None = None) -> None:
        from config.config import reports_path

        super().__init__({})
        self.cache_dir = Path(cache_dir) if cache_dir is not None else reports_path("history_cache")

    def bars_from(self, symbol, con_id, at, timeframe):
        key = f"{symbol}|{con_id}|{timeframe}"
        if key not in self.data:
            profile = self.PROFILE_BY_TIMEFRAME.get(timeframe)
            # A reused ticker is stored per conId (run_fetch_journal_bars.cache_file). Never read a
            # plain SYMBOL file when per-conId files exist: it may belong to the other company.
            by_con = self.cache_dir / f"{symbol}.{con_id}_{profile}.json"
            plain = self.cache_dir / f"{symbol}_{profile}.json"
            shared = any(self.cache_dir.glob(f"{symbol}.*_{profile}.json")) if profile else False
            path = by_con if by_con.exists() else (plain if not shared else by_con)
            bars = []
            if profile and path.exists():
                raw = json.loads(path.read_text(encoding="utf-8"))
                bars = [PriceBar(r["time"], float(r["open"]), float(r["high"]), float(r["low"]),
                                 float(r["close"]), float(r.get("volume", 0.0))) for r in raw]
            self.data[key] = bars
        cutoff = _naive(at)
        return [b for b in self.data[key] if cutoff is not None and _naive(b.time) >= cutoff]


@dataclass(frozen=True)
class ScoredOutcome:
    decision_id: int
    status: str             # FINAL | PENDING_DATA | NOT_EVALUABLE
    evaluated: str | None   # SELECTED | COUNTERFACTUAL | None
    strategy: str | None
    side: str | None
    filled: bool
    exit_reason: str | None
    return_pct: float | None
    mae_pct: float | None
    mfe_pct: float | None
    bars_held: int
    forward: dict[int, float | None]
    executable: bool = False


TRADE_ACTION_PREFIXES = ("WOULD_", "APPROVED_", "PAPER_", "PORTFOLIO_REJECTED", "SHADOW_")


def _tick(price: float) -> float:
    from decimal import ROUND_HALF_UP, Decimal

    return float(Decimal(str(price)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def align(row: sqlite3.Row, bars_from: list[PriceBar]) -> list[PriceBar] | None:
    """Bars usable for evaluation, or None if the series cannot be aligned.

    The provider series must START with the decision bar itself (proves contiguity:
    the next element is the true successor). Only bars that START at or after the
    moment the decision was recorded (``created_at``) are usable: price action before
    the decision existed cannot be traded.
    """
    decision_bar = _naive(row["bar_time"])
    if decision_bar is None or not bars_from or _naive(bars_from[0].time) != decision_bar:
        return None
    decided_at = _naive(row["created_at"])
    later = bars_from[1:]
    if decided_at is None:
        return later
    return [b for b in later if _naive(b.time) >= decided_at]


def _protocol_fingerprint() -> str | None:
    """Fingerprint of the scoring rules that produced a row (research/fwd_protocol.py)."""
    try:
        from research.fwd_protocol import protocol_fingerprint
        return protocol_fingerprint()
    except Exception:
        return None


def _session_date(created_at):
    """Exchange (ET) date on which a decision was taken; None if unknown."""
    from zoneinfo import ZoneInfo

    dt = as_datetime(created_at)
    if dt is None or dt.tzinfo is None:
        return None
    return dt.astimezone(ZoneInfo("America/New_York")).date()


def evaluate_row(row: sqlite3.Row, bars_after: list[PriceBar], costs: CostModel = BASELINE) -> ScoredOutcome:
    """Pure evaluation of one decision row against bars that came after the decision."""
    action = str(row["action"] or "")
    selected = row["strategy"] is not None and row["side"] not in (None, "FLAT") and action.startswith(TRADE_ACTION_PREFIXES)
    if selected:
        kind, strategy, side, stop, target, limit = ("SELECTED", row["strategy"], row["side"], row["stop"],
                                                     row["target"], row["entry"])
    elif row["top_side"] in ("LONG", "SHORT") and row["top_stop"] is not None and row["top_target"] is not None:
        kind, strategy, side, stop, target, limit = ("COUNTERFACTUAL", row["top_strategy"], row["top_side"],
                                                     row["top_stop"], row["top_target"], row["top_entry"])
    else:
        kind = strategy = side = stop = target = limit = None

    # Forward returns start at the first tradeable price: the open of the first bar that
    # started at/after the decision (``bars_after`` is already cut at created_at by ``align``).
    keys = row.keys()
    reference = float(bars_after[0].open) if bars_after and float(bars_after[0].open) > 0 else None
    forward: dict[int, float | None] = {}
    for h in FORWARD_HORIZONS:
        forward[h] = (bars_after[h - 1].close / reference - 1) * 100 if reference and len(bars_after) >= h else None

    if kind is None:
        if reference is None:
            status = "NOT_EVALUABLE"  # no signal and no reference price was recorded
        elif all(v is not None for v in forward.values()):
            status = "FINAL"
        else:
            status = "PENDING_DATA"
        return ScoredOutcome(int(row["id"]), status, None, None, None, False, None, None, None, None, 0, forward)

    complete_forward = all(v is not None for v in forward.values())
    market_open = row["market_open"] if "market_open" in keys else None
    executable = kind == "SELECTED" and is_executable(action, market_open)
    if kind == "SELECTED" and not executable:
        # Selector output that would NOT have been transmitted (blocked, rejected, session
        # closed): forward returns only — never a credited fill.
        status = "FINAL" if complete_forward else "PENDING_DATA"
        return ScoredOutcome(int(row["id"]), status, kind, strategy, side, False, "not_executable",
                             None, None, None, 0, forward, False)
    out = score_decision(side, _tick(float(stop)), _tick(float(target)), bars_after, costs, MAX_BRACKET_BARS,
                         limit=None if limit is None else _tick(float(limit)),
                         valid_on=_session_date(row["created_at"]))
    status = "FINAL" if (bars_after and out.resolved and complete_forward) else "PENDING_DATA"
    return ScoredOutcome(int(row["id"]), status, kind, strategy, side, out.filled, out.exit_reason,
                         out.return_pct, out.mae_pct, out.mfe_pct, out.bars_held, forward, executable)


class ShadowScorer:
    def __init__(self, db_path: str | Path | None = None, costs: CostModel = BASELINE) -> None:
        self.path = Path(db_path) if db_path is not None else state_path("strategy_performance.db")
        self.costs = costs
        ShadowJournal(self.path)  # ensures the decision tables exist (append-only schema)
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS shadow_outcomes (
                    decision_id INTEGER NOT NULL,
                    scorer_version TEXT NOT NULL,
                    scored_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    evaluated TEXT,
                    strategy TEXT,
                    side TEXT,
                    filled INTEGER,
                    exit_reason TEXT,
                    return_pct REAL,
                    mae_pct REAL,
                    mfe_pct REAL,
                    bars_held INTEGER,
                    fwd_1 REAL,
                    fwd_5 REAL,
                    fwd_20 REAL,
                    cost_model TEXT,
                    PRIMARY KEY (decision_id, scorer_version)
                )
                """
            )
            existing = {r[1] for r in conn.execute("PRAGMA table_info(shadow_outcomes)")}
            for name, ddl in (("executable", "INTEGER"), ("provider", "TEXT"), ("protocol_fingerprint", "TEXT")):
                if name not in existing:
                    conn.execute("ALTER TABLE shadow_outcomes ADD COLUMN " + name + " " + ddl)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def pending_rows(self, min_created_at: str | None = None) -> list[sqlite3.Row]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT d.*, c.market_open AS market_open FROM shadow_decisions d
                   LEFT JOIN discovery_cycles c ON c.cycle_id=d.cycle_id
                   LEFT JOIN shadow_outcomes o ON o.decision_id=d.id AND o.scorer_version=?
                   WHERE o.decision_id IS NULL OR o.status='PENDING_DATA'
                   ORDER BY d.id""",
                (SCORER_VERSION,),
            ).fetchall()
        if min_created_at is None:
            return rows
        floor = _naive(min_created_at)
        return [r for r in rows if _naive(r["created_at"]) is not None and _naive(r["created_at"]) >= floor]

    async def score_pending(self, provider, limit: int | None = None, now: datetime | None = None,
                            min_created_at: str | None = None) -> dict[str, int]:
        counts = {"FINAL": 0, "PENDING_DATA": 0, "NOT_EVALUABLE": 0, "DUPLICATE": 0, "PROVIDER_ERROR": 0}
        provider_name = getattr(provider, "name", None) or type(provider).__name__
        now = _naive(now or datetime.now(timezone.utc))
        rows = self.pending_rows(min_created_at)
        if limit is not None:
            rows = rows[:limit]
        for row in rows:
            if "duplicate_of" in row.keys() and row["duplicate_of"] is not None:
                self._upsert(ScoredOutcome(int(row["id"]), "DUPLICATE", None, None, None, False, None,
                                           None, None, None, 0, {}), provider_name)
                counts["DUPLICATE"] += 1
                continue
            if str(row["action"] or "") in ("CANDIDATE_ERROR", "INSTRUMENT_EXCLUDED") or row["bar_time"] is None:
                self._upsert(ScoredOutcome(int(row["id"]), "NOT_EVALUABLE", None, None, None, False,
                                           "no_decision_bar", None, None, None, 0, {}), provider_name)
                counts["NOT_EVALUABLE"] += 1
                continue
            try:
                bars = provider.bars_from(row["symbol"], row["con_id"], row["bar_time"], row["timeframe"])
                if inspect.isawaitable(bars):
                    bars = await bars
            except Exception:
                # One provider failure must not stop the run; the row stays pending.
                counts["PROVIDER_ERROR"] += 1
                continue
            usable = align(row, list(bars or []))
            if usable is None:
                created = _naive(row["created_at"])
                # Only a provider that can actually serve the bar (IBKR) may give up on a row;
                # a local cache that lacks it (holdout cache, late fetch) leaves it pending.
                authoritative = bool(getattr(provider, "authoritative", False))
                expired = (authoritative and created is not None and now is not None
                           and (now - created).days >= UNALIGNED_EXPIRY_DAYS)
                outcome = ScoredOutcome(int(row["id"]), "NOT_EVALUABLE" if expired else "PENDING_DATA", None, None,
                                        None, False, "unaligned_series_expired" if expired else "unaligned_series",
                                        None, None, None, 0, {})
            else:
                outcome = evaluate_row(row, usable, self.costs)
            self._upsert(outcome, provider_name)
            counts[outcome.status] += 1
        return counts

    def _upsert(self, o: ScoredOutcome, provider: str | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO shadow_outcomes (decision_id, scorer_version, scored_at, status, evaluated,
                   strategy, side, filled, exit_reason, return_pct, mae_pct, mfe_pct, bars_held,
                   fwd_1, fwd_5, fwd_20, cost_model, executable, provider, protocol_fingerprint)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(decision_id, scorer_version) DO UPDATE SET
                     scored_at=excluded.scored_at, status=excluded.status, evaluated=excluded.evaluated,
                     strategy=excluded.strategy, side=excluded.side, filled=excluded.filled,
                     exit_reason=excluded.exit_reason, return_pct=excluded.return_pct,
                     mae_pct=excluded.mae_pct, mfe_pct=excluded.mfe_pct, bars_held=excluded.bars_held,
                     fwd_1=COALESCE(shadow_outcomes.fwd_1, excluded.fwd_1),
                     fwd_5=COALESCE(shadow_outcomes.fwd_5, excluded.fwd_5),
                     fwd_20=COALESCE(shadow_outcomes.fwd_20, excluded.fwd_20),
                     cost_model=excluded.cost_model, executable=excluded.executable,
                     provider=COALESCE(shadow_outcomes.provider, excluded.provider),
                     protocol_fingerprint=COALESCE(shadow_outcomes.protocol_fingerprint, excluded.protocol_fingerprint)
                   WHERE shadow_outcomes.status != 'FINAL'""",
                (o.decision_id, SCORER_VERSION, datetime.now(timezone.utc).isoformat(), o.status, o.evaluated,
                 o.strategy, o.side, int(o.filled), o.exit_reason, o.return_pct, o.mae_pct, o.mfe_pct,
                 o.bars_held, o.forward.get(1), o.forward.get(5), o.forward.get(20), self.costs.name,
                 int(bool(o.executable)), provider, _protocol_fingerprint()),
            )

    def counts(self) -> dict:
        """Outcome COUNTS only (by status and arm): safe to look at during an open FWD window."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status, evaluated, COUNT(*) FROM shadow_outcomes WHERE scorer_version=? "
                "GROUP BY status, evaluated", (SCORER_VERSION,)).fetchall()
        return {"scorer_version": SCORER_VERSION,
                "by_status_and_arm": {f"{s}|{e or '-'}": n for s, e, n in rows}}

    def report(self, min_cycle_rows: int = MIN_CYCLE_ROWS) -> dict:
        """Aggregate FINAL outcomes: selected trades vs NO_TRADE counterfactuals, by regime/strategy.

        UNBLINDED outcome statistics. While an FWD window is open, reading this is an interim look
        (docs/FWD_PROTOCOL.md): use ``counts()`` instead, or record the look in LOG.md."""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT o.*, d.action, d.regime, d.symbol, d.cycle_id, d.side AS decision_side FROM shadow_outcomes o
                   JOIN shadow_decisions d ON d.id=o.decision_id
                   WHERE o.status='FINAL' AND o.scorer_version=?""",
                (SCORER_VERSION,),
            ).fetchall()

        def summary(items):
            rets = [r["return_pct"] for r in items if r["filled"] and r["return_pct"] is not None]
            return {
                "n": len(items), "filled": len(rets),
                "mean_net_return_pct": (sum(rets) / len(rets)) if rets else None,
                "win_rate_pct": (sum(x > 0 for x in rets) / len(rets) * 100) if rets else None,
            }

        def action_class(action: str) -> str:
            for prefix, name in (("PAPER_SUBMITTED", "paper_submitted"), ("PAPER_", "paper_blocked"),
                                 ("SHADOW_SUBMIT", "shadow_submit"), ("SHADOW_BLOCKED", "shadow_blocked"),
                                 ("APPROVED_", "approved"), ("PORTFOLIO_REJECTED", "portfolio_rejected"),
                                 ("WOULD_", "would_trade")):
                if action.startswith(prefix):
                    return name
            return "other"

        selected = [r for r in rows if r["evaluated"] == "SELECTED"]
        executable = [r for r in selected if r["executable"]]
        counterfactual = [r for r in rows if r["evaluated"] == "COUNTERFACTUAL"]
        rejected_good = [r for r in counterfactual if r["filled"] and (r["return_pct"] or 0) > 0]
        by_regime: dict[str, list] = {}
        by_action: dict[str, list] = {}
        for r in selected:
            by_regime.setdefault(r["regime"], []).append(r)
            by_action.setdefault(action_class(str(r["action"])), []).append(r)
        return {
            "final_outcomes": len(rows),
            "selected": summary(selected),
            # Only decisions that would really have been transmitted can carry fills/P&L.
            "selected_executable": summary(executable),
            "selected_not_executable": len(selected) - len(executable),
            "no_trade_counterfactual": summary(counterfactual),
            "no_trade_rejected_profitable": len(rejected_good),
            "selected_by_regime": {k: summary(v) for k, v in sorted(by_regime.items())},
            "selected_by_action": {k: summary(v) for k, v in sorted(by_action.items())},
            "selected_vs_cycle_fwd5": _excess_vs_cycle(rows, "SELECTED", min_cycle_rows=min_cycle_rows),
            "counterfactual_vs_cycle_fwd5": _excess_vs_cycle(rows, "COUNTERFACTUAL", min_cycle_rows=min_cycle_rows),
            "selected_long_vs_cycle_fwd5": _excess_vs_cycle(rows, "SELECTED", side="LONG",
                                                            min_cycle_rows=min_cycle_rows),
        }


def _excess_vs_cycle(rows, evaluated: str, side: str | None = None, min_cycle_rows: int = MIN_CYCLE_ROWS) -> dict:
    """FWD1/FWD2 measure: side-adjusted GROSS 5-bar forward return minus the LEAVE-ONE-OUT mean
    5-bar forward return of the other scored decisions in the same discovery cycle
    (point-in-time cohort benchmark; the pick is never part of its own benchmark). Cycles with
    fewer than ``min_cycle_rows`` scored rows are excluded (reported). Costs (~9 bps round
    trip) are NOT subtracted here. The t-stat is over per-cycle means; consecutive hourly
    cycles overlap in their 5-bar windows, so this t is OPTIMISTIC — the FWD protocol
    clusters by trading day with Newey-West (docs/FWD_PROTOCOL.md)."""
    from math import sqrt

    by_cycle: dict[str, list] = {}
    for r in rows:
        if r["fwd_5"] is not None:
            by_cycle.setdefault(r["cycle_id"], []).append(r)
    cycle_means = []
    n_events = 0
    excluded = 0
    for cycle_rows in by_cycle.values():
        if len(cycle_rows) < max(2, int(min_cycle_rows)):
            excluded += 1
            continue
        total = sum(r["fwd_5"] for r in cycle_rows)
        picked = []
        for r in cycle_rows:
            if r["evaluated"] != evaluated or (side is not None and r["side"] != side):
                continue
            bench = (total - r["fwd_5"]) / (len(cycle_rows) - 1)
            picked.append((-1.0 if r["side"] == "SHORT" else 1.0) * (r["fwd_5"] - bench))
        if picked:
            n_events += len(picked)
            cycle_means.append(sum(picked) / len(picked))
    n = len(cycle_means)
    base = {"n": n_events, "cycles": n, "cycles_excluded_small": excluded}
    if n == 0:
        return {**base, "mean_excess_pct": None, "t": None}
    m = sum(cycle_means) / n
    if n < 2:
        return {**base, "mean_excess_pct": m, "t": None}
    var = sum((x - m) ** 2 for x in cycle_means) / (n - 1)
    return {**base, "mean_excess_pct": m, "t": (m / sqrt(var / n)) if var > 0 else None}
