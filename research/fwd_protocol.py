"""Pre-registered forward evaluation of FWD1 / FWD2 (docs/FWD_PROTOCOL.md). Offline, read-only.

The rules below are FIXED; changing any constant is a new pre-registration (new id, new
evidence window), never an edit of this one.

Window: from the first market-open shadow_only cycle journaled with DECISION_VERSION v2 up to
the BINDING CUTOFF = the first trading day on which every sample minimum holds, or
EVIDENCE_DEADLINE, whichever comes first. Data after the cutoff is never used (no optional
stopping); before the cutoff the output is "monitoring only", never a decision.

Population: canonical decisions of that window (v2, shadow_only, learning frozen) with a
scorer-v3 5-bar forward return (FINAL or PENDING_DATA), market open at the cycle, cycle free of
BLOCKING quality gates, decided at most MAX_DECISION_LAG after the decision bar completed, in
cycles with ≥ MIN_CYCLE_ROWS such rows.

Measure: side-adjusted GROSS 5-bar forward return (from the first tradeable price after the
decision) minus the LEAVE-ONE-OUT mean of the other rows of the same cycle. Inference:
per-trading-day (ET) means; standard error with uniform (Hansen–Hodrick) weights at lag 1
day, never below the i.i.d. one; critical values from Student t with days − 1 df.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from math import exp, lgamma, log, sqrt
from pathlib import Path
from zoneinfo import ZoneInfo

from research import shadow_quality
from research.shadow_journal import DECISION_VERSION
from research.shadow_scoring import MIN_CYCLE_ROWS, SCORER_VERSION

PROTOCOL_ID = "FWD-v1"
ROUND_TRIP_COST_PCT = 0.093          # 9.3 bps, BASELINE cost model round trip
FAMILY_K = 3                         # FWD1, FWD2, FWD3
ALPHA = 0.05                         # two-sided, Bonferroni over FAMILY_K
MIN_EVENTS = 300
MIN_DAYS = 40
MIN_SYMBOLS = 60
MIN_PAIRED_DAYS = 40
MAX_MISSING_SHARE = 0.10             # population rows without a forward return
MAX_DECISION_LAG = timedelta(minutes=80)
EVIDENCE_DEADLINE = date(2027, 3, 31)
_ET = ZoneInfo("America/New_York")


# ---- Student t (no scipy): regularized incomplete beta via continued fractions -------------
def _betacf(a: float, b: float, x: float) -> float:
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > 1e-300 else 1e-300)
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > 1e-300 else 1e-300)
        c = 1.0 + aa / c if abs(1.0 + aa / c) > 1e-300 else 1e-300
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > 1e-300 else 1e-300)
        c = 1.0 + aa / c if abs(1.0 + aa / c) > 1e-300 else 1e-300
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-12:
            break
    return h


def _betainc(a: float, b: float, x: float) -> float:
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    bt = exp(lgamma(a + b) - lgamma(a) - lgamma(b) + a * log(x) + b * log(1 - x))
    if x < (a + 1) / (a + b + 2):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1 - x) / b


def t_cdf(t: float, df: int) -> float:
    x = df / (df + t * t)
    tail = 0.5 * _betainc(df / 2.0, 0.5, x)
    return 1.0 - tail if t > 0 else tail


def t_quantile(p: float, df: int) -> float:
    lo, hi = -50.0, 50.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if t_cdf(mid, df) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def critical_t(days: int) -> float:
    """Two-sided Bonferroni critical value (alpha/K) with days - 1 degrees of freedom."""
    return t_quantile(1 - ALPHA / FAMILY_K / 2, max(1, days - 1))


# ---- data -------------------------------------------------------------------------------------
def _utc(value):
    return shadow_quality._utc(value)


def _population(path: Path, run_mode: str = "shadow_only"):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    with conn:
        rows = conn.execute(
            """SELECT d.id, d.cycle_id, d.symbol, d.created_at, d.bar_time, d.action, d.strategy, d.top_side,
                      o.status, o.evaluated, o.side, o.fwd_5, c.market_open, c.created_at AS cycle_created
               FROM shadow_decisions d
               JOIN discovery_cycles c ON c.cycle_id = d.cycle_id
               LEFT JOIN shadow_outcomes o ON o.decision_id = d.id AND o.scorer_version = ?
               WHERE d.duplicate_of IS NULL AND d.decision_version = ? AND d.run_mode = ?
                 AND COALESCE(d.learning_mode, '') = 'frozen' AND c.market_open = 1
                 AND d.action != 'CANDIDATE_ERROR'
               ORDER BY d.created_at""",
            (SCORER_VERSION, DECISION_VERSION, run_mode)).fetchall()
    conn.close()
    kept = []
    for r in rows:
        created, bar = _utc(r["created_at"]), _utc(r["bar_time"])
        if created is None or bar is None or created - (bar + timedelta(hours=1)) > MAX_DECISION_LAG:
            continue  # stale decision bar: its forward window is not what the runtime could trade
        kept.append(r)
    return kept


def _day(row) -> str:
    return _utc(row["created_at"]).astimezone(_ET).date().isoformat()


def _excess_events(rows, evaluated: str, side: str = "LONG"):
    by_cycle: dict[str, list] = {}
    for r in rows:
        if r["fwd_5"] is not None and r["status"] in ("FINAL", "PENDING_DATA"):
            by_cycle.setdefault(r["cycle_id"], []).append(r)
    events = []
    for cycle_rows in by_cycle.values():
        if len(cycle_rows) < MIN_CYCLE_ROWS:
            continue
        total = sum(r["fwd_5"] for r in cycle_rows)
        for r in cycle_rows:
            if r["evaluated"] != evaluated or r["side"] != side:
                continue
            bench = (total - r["fwd_5"]) / (len(cycle_rows) - 1)
            events.append((_day(r), r["symbol"], r["fwd_5"] - bench))
    return events


def _daily(events) -> dict[str, float]:
    days: dict[str, list[float]] = {}
    for day, _, x in events:
        days.setdefault(day, []).append(x)
    return {d: sum(v) / len(v) for d, v in sorted(days.items())}


def _stats(series: list[float]) -> dict:
    n = len(series)
    if n < 3:
        return {"days": n, "mean_pct": (sum(series) / n) if n else None, "se_pct": None, "t": None}
    m = sum(series) / n
    d = [x - m for x in series]
    g0 = sum(x * x for x in d) / n
    g1 = sum(d[t] * d[t - 1] for t in range(1, n)) / n
    var = max(g0 + 2 * g1, g0)          # uniform weights at lag 1, never below i.i.d.
    se = sqrt(var / n) if var > 0 else None
    return {"days": n, "mean_pct": m, "se_pct": se, "t": (m / se) if se else None}


def _sd(series: list[float]) -> float | None:
    if len(series) < 2:
        return None
    m = sum(series) / len(series)
    return sqrt(sum((x - m) ** 2 for x in series) / (len(series) - 1))


def _window(path: Path, run_mode: str, minimums) -> tuple[list, dict]:
    """Rows up to the binding cutoff; the cutoff itself; quality verdict of that window."""
    rows = _population(path, run_mode)
    if not rows:
        return [], {"window_start": None, "cutoff_day": None, "binding": False, "reason": "no data"}
    days = sorted({_day(r) for r in rows})
    cutoff_day, binding = None, False
    for day in days:
        subset = [r for r in rows if _day(r) <= day]
        if minimums(subset):
            cutoff_day, binding = day, True
            break
    if cutoff_day is None:
        today = datetime.now(_ET).date()
        if today > EVIDENCE_DEADLINE:
            cutoff_day, binding = EVIDENCE_DEADLINE.isoformat(), True
        else:
            cutoff_day = days[-1]
    rows = [r for r in rows if _day(r) <= cutoff_day]
    # Quality window = exactly the cycles that produced this population (by cycle time).
    cycle_times = [_utc(r["cycle_created"]) for r in rows if _utc(r["cycle_created"]) is not None]
    start = min(cycle_times)
    until = max(cycle_times) + timedelta(microseconds=1)
    _, session = shadow_quality.evaluate(path, since=start, until=until, run_mode=run_mode)
    excluded = set(session["excluded_cycles"])
    rows = [r for r in rows if r["cycle_id"] not in excluded]
    return rows, {"window_start": start.isoformat(), "cutoff_day": cutoff_day, "binding": binding,
                  "quality": session["verdict"], "quality_reasons": session["reasons"],
                  "code_versions": session.get("code_versions"), "config_hashes": session.get("config_hashes")}


def _missing_share(rows) -> float | None:
    if not rows:
        return None
    missing = sum(1 for r in rows if r["fwd_5"] is None or r["status"] not in ("FINAL", "PENDING_DATA"))
    return missing / len(rows)


def _fwd1_minimums(rows) -> bool:
    events = _excess_events(rows, "SELECTED")
    return (len(events) >= MIN_EVENTS and len({d for d, _, _ in events}) >= MIN_DAYS
            and len({s for _, s, _ in events}) >= MIN_SYMBOLS)


def _fwd2_minimums(rows) -> bool:
    sel, cf = _excess_events(rows, "SELECTED"), _excess_events(rows, "COUNTERFACTUAL")
    paired = set(_daily(sel)) & set(_daily(cf))
    return len(sel) >= MIN_EVENTS and len(cf) >= MIN_EVENTS and len(paired) >= MIN_PAIRED_DAYS


def _gate(window: dict, missing: float | None, enough: bool) -> str | None:
    if window.get("quality") != "VALID_FOR_RESEARCH":
        return f"evidence window invalid: {window.get('quality_reasons')}"
    if missing is not None and missing > MAX_MISSING_SHARE:
        return f"missing forward returns {missing:.1%} > {MAX_MISSING_SHARE:.0%}"
    if not window.get("binding"):
        return "monitoring only: minimum sample not reached and deadline not passed"
    if not enough:
        return "deadline reached without the minimum sample"
    return None


def evaluate_fwd1(path: str | Path, run_mode: str = "shadow_only") -> dict:
    rows, window = _window(Path(path), run_mode, _fwd1_minimums)
    events = _excess_events(rows, "SELECTED")
    daily = _daily(events)
    gross = _stats(list(daily.values()))
    net = _stats([x - ROUND_TRIP_COST_PCT for x in daily.values()])
    sample = {"events": len(events), "days": len(daily), "symbols": len({s for _, s, _ in events})}
    missing = _missing_share(rows)
    decision, reason = "INCONCLUSIVE", _gate(window, missing, _fwd1_minimums(rows))
    if reason is None and gross["se_pct"] is not None and net["t"] is not None:
        crit = critical_t(len(daily))
        upper = gross["mean_pct"] + t_quantile(0.975, len(daily) - 1) * gross["se_pct"]
        if net["mean_pct"] > 0 and net["t"] >= crit:
            decision, reason = "KEEP", "selected LONG beat the same-cycle cohort after costs (contradicts F6)"
        elif upper < ROUND_TRIP_COST_PCT:
            decision, reason = "REJECT", "upper 97.5% bound of gross excess below round-trip cost (confirms F6)"
        else:
            reason = "neither threshold crossed"
    elif reason is None:
        reason = "statistics not computable"
    sample["daily_sd_pct"] = _sd(list(daily.values()))
    return {"protocol": PROTOCOL_ID, "test": "FWD1", "decision": decision, "reason": reason, "sample": sample,
            "gross_excess": gross, "net_excess": net, "missing_share": missing, "window": window,
            "critical_t": critical_t(len(daily)) if daily else None}


def evaluate_fwd2(path: str | Path, run_mode: str = "shadow_only") -> dict:
    rows, window = _window(Path(path), run_mode, _fwd2_minimums)
    sel_events, cf_events = _excess_events(rows, "SELECTED"), _excess_events(rows, "COUNTERFACTUAL")
    sel, cf = _daily(sel_events), _daily(cf_events)
    paired = [sel[d] - cf[d] for d in sorted(set(sel) & set(cf))]
    diff = _stats(paired)
    missing = _missing_share(rows)
    decision, reason = "INCONCLUSIVE", _gate(window, missing, _fwd2_minimums(rows))
    if reason is None and diff["se_pct"] is not None and diff["t"] is not None:
        crit = critical_t(len(paired))
        half_width = t_quantile(0.975, len(paired) - 1) * diff["se_pct"]
        if diff["mean_pct"] > 0 and diff["t"] >= crit:
            decision, reason = "KEEP", "the threshold separates better setups from the ones it passes on"
        elif abs(diff["mean_pct"]) + half_width < ROUND_TRIP_COST_PCT:
            decision, reason = "REJECT", "95% interval of the difference within +/- round-trip cost (hypothesis supported)"
        else:
            reason = "neither threshold crossed"
    elif reason is None:
        reason = "statistics not computable"
    return {"protocol": PROTOCOL_ID, "test": "FWD2", "decision": decision, "reason": reason,
            "sample": {"selected": len(sel_events), "counterfactual": len(cf_events), "paired_days": len(paired),
                       "daily_sd_pct": _sd(paired)},
            "difference": diff, "missing_share": missing, "window": window,
            "critical_t": critical_t(len(paired)) if paired else None}
