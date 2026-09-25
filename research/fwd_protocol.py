"""Pre-registered forward evaluation of FWD1 / FWD2 (docs/FWD_PROTOCOL.md). Offline, read-only.

The rules below are FIXED; changing any constant is a new pre-registration (new id, new
evidence window), never an edit of this one.

Population (both): canonical (non-duplicate) decisions with DECISION_VERSION v2, run mode
shadow_only, learning frozen, scorer v2 FINAL with fwd_5, in cycles that passed every BLOCKING
quality gate, market open at the cycle, and cycles with ≥ MIN_CYCLE_ROWS scored rows.

Measure: side-adjusted GROSS 5-bar forward return minus the LEAVE-ONE-OUT mean of the other
scored rows of the same cycle. Inference: per-trading-day (ET) means, Newey-West t with lag 1
day (5-bar windows spill into the next session). Net = gross − ROUND_TRIP_COST_PCT.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from math import sqrt
from pathlib import Path
from zoneinfo import ZoneInfo

from research import shadow_quality
from research.event_study import newey_west_t
from research.shadow_journal import DECISION_VERSION
from research.shadow_scoring import MIN_CYCLE_ROWS, SCORER_VERSION

PROTOCOL_ID = "FWD-v1"
ROUND_TRIP_COST_PCT = 0.093          # 9.3 bps, BASELINE cost model round trip
T_CRITICAL = 2.40                    # K = 3 forward tests, two-sided alpha 0.05 (Bonferroni)
Z_975 = 1.96
NW_LAG_DAYS = 1
MIN_EVENTS = 300
MIN_DAYS = 40
MIN_SYMBOLS = 60
MIN_PAIRED_DAYS = 40
EVIDENCE_DEADLINE = "2027-03-31"     # evaluate at the minimum sample or this date, whichever first
_ET = ZoneInfo("America/New_York")


def _rows(path: Path, run_mode: str = "shadow_only"):
    results, session = shadow_quality.evaluate(path, run_mode=run_mode)
    excluded = set(session["excluded_cycles"])
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    with conn:
        rows = conn.execute(
            """SELECT d.id, d.cycle_id, d.symbol, d.created_at, d.side AS decision_side, o.evaluated, o.side,
                      o.fwd_5, c.market_open
               FROM shadow_outcomes o
               JOIN shadow_decisions d ON d.id = o.decision_id
               JOIN discovery_cycles c ON c.cycle_id = d.cycle_id
               WHERE o.scorer_version=? AND o.status='FINAL' AND o.fwd_5 IS NOT NULL
                 AND d.duplicate_of IS NULL AND d.decision_version=? AND d.run_mode=?
                 AND COALESCE(d.learning_mode, '')='frozen' AND c.market_open=1""",
            (SCORER_VERSION, DECISION_VERSION, run_mode)).fetchall()
    conn.close()
    return [r for r in rows if r["cycle_id"] not in excluded], session


def _excess_events(rows, evaluated: str, side: str = "LONG"):
    by_cycle: dict[str, list] = {}
    for r in rows:
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
            day = datetime.fromisoformat(r["created_at"]).astimezone(_ET).date().isoformat()
            events.append((day, r["symbol"], r["fwd_5"] - bench))
    return events


def _daily(events) -> dict[str, float]:
    days: dict[str, list[float]] = {}
    for day, _, x in events:
        days.setdefault(day, []).append(x)
    return {d: sum(v) / len(v) for d, v in sorted(days.items())}


def _stats(series: list[float]) -> dict:
    n = len(series)
    if n < 2:
        return {"days": n, "mean_pct": series[0] if series else None, "nw_t": None, "se_pct": None}
    mean = sum(series) / n
    t = newey_west_t(series, NW_LAG_DAYS)
    se = abs(mean / t) if t not in (None, 0) else None
    return {"days": n, "mean_pct": mean, "nw_t": t, "se_pct": se}


def evaluate_fwd1(path: str | Path, run_mode: str = "shadow_only") -> dict:
    rows, session = _rows(Path(path), run_mode)
    events = _excess_events(rows, "SELECTED")
    daily = _daily(events)
    gross = _stats(list(daily.values()))
    net_series = [x - ROUND_TRIP_COST_PCT for x in daily.values()]
    net = _stats(net_series)
    sample = {"events": len(events), "days": len(daily), "symbols": len({s for _, s, _ in events})}
    enough = sample["events"] >= MIN_EVENTS and sample["days"] >= MIN_DAYS and sample["symbols"] >= MIN_SYMBOLS
    decision, reason = "INCONCLUSIVE", "minimum sample not reached"
    if session["verdict"] != "VALID_FOR_RESEARCH":
        reason = f"evidence window invalid: {session['reasons']}"
    elif enough and net["mean_pct"] is not None and net["nw_t"] is not None:
        upper = gross["mean_pct"] + Z_975 * (gross["se_pct"] or 0.0)
        if net["mean_pct"] > 0 and net["nw_t"] >= T_CRITICAL:
            decision, reason = "KEEP", "selected LONG beat the same-cycle cohort after costs (contradicts F6)"
        elif upper < ROUND_TRIP_COST_PCT:
            decision, reason = "REJECT", "upper 97.5% bound of gross excess below round-trip cost (confirms F6)"
        else:
            reason = "neither threshold crossed"
    return {"protocol": PROTOCOL_ID, "test": "FWD1", "decision": decision, "reason": reason, "sample": sample,
            "gross_excess": gross, "net_excess": net, "quality": session["verdict"]}


def evaluate_fwd2(path: str | Path, run_mode: str = "shadow_only") -> dict:
    rows, session = _rows(Path(path), run_mode)
    sel = _daily(_excess_events(rows, "SELECTED"))
    cf = _daily(_excess_events(rows, "COUNTERFACTUAL"))
    n_sel = len(_excess_events(rows, "SELECTED"))
    n_cf = len(_excess_events(rows, "COUNTERFACTUAL"))
    paired = [sel[d] - cf[d] for d in sorted(set(sel) & set(cf))]
    diff = _stats(paired)
    enough = n_sel >= MIN_EVENTS and n_cf >= MIN_EVENTS and len(paired) >= MIN_PAIRED_DAYS
    decision, reason = "INCONCLUSIVE", "minimum sample not reached"
    if session["verdict"] != "VALID_FOR_RESEARCH":
        reason = f"evidence window invalid: {session['reasons']}"
    elif enough and diff["mean_pct"] is not None and diff["nw_t"] is not None:
        half_width = Z_975 * (diff["se_pct"] or 0.0)
        if diff["mean_pct"] > 0 and diff["nw_t"] >= T_CRITICAL:
            decision, reason = "KEEP", "the threshold separates better setups from the ones it passes on"
        elif abs(diff["mean_pct"]) + half_width < ROUND_TRIP_COST_PCT:
            decision, reason = "REJECT", "95% interval of the difference within +/- round-trip cost (hypothesis supported)"
        else:
            reason = "neither threshold crossed"
    return {"protocol": PROTOCOL_ID, "test": "FWD2", "decision": decision, "reason": reason,
            "sample": {"selected": n_sel, "counterfactual": n_cf, "paired_days": len(paired)},
            "difference": diff, "quality": session["verdict"]}
