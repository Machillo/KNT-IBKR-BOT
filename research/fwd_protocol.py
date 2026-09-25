"""Pre-registered forward evaluation of FWD1 / FWD2 (docs/FWD_PROTOCOL.md). Offline, read-only.

The rules below are FIXED; changing any constant is a new pre-registration (new id, new
evidence window), never an edit of this one.

Window: starts at the REGISTERED start (``register_window``: a file next to the journal,
written once, recording the start time, the decision-code fingerprint and the config hash).
BINDING CUTOFF = the first trading day (never after EVIDENCE_DEADLINE) on which every sample
minimum holds, counted from the DECISIONS (not from how far scoring has got), after the
per-cycle quality exclusions; or the deadline. Data after the cutoff is never used. The
result is binding only once every row up to the cutoff has been scored; before that the output
is "monitoring only", never a decision.

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

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from math import exp, lgamma, log, sqrt
from pathlib import Path
from zoneinfo import ZoneInfo

from research import shadow_quality
from research.shadow_journal import DECISION_VERSION
from research.shadow_scoring import MIN_CYCLE_ROWS, SCORER_VERSION, TRADE_ACTION_PREFIXES

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
            """SELECT d.id, d.cycle_id, d.symbol, d.created_at, d.bar_time, d.action, d.reason, d.strategy, d.side AS d_side,
                      d.top_side, d.top_stop, d.top_target, o.status, o.evaluated, o.side, o.fwd_5, c.market_open,
                      c.created_at AS cycle_created, c.decision_fingerprint, c.config_hash
               FROM shadow_decisions d
               JOIN discovery_cycles c ON c.cycle_id = d.cycle_id
               LEFT JOIN shadow_outcomes o ON o.decision_id = d.id AND o.scorer_version = ?
               WHERE d.duplicate_of IS NULL AND d.decision_version = ? AND d.run_mode = ?
                 AND COALESCE(d.learning_mode, '') = 'frozen' AND c.market_open = 1
                 AND d.action NOT IN ('CANDIDATE_ERROR', 'INSTRUMENT_EXCLUDED')
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


def window_file(path: Path) -> Path:
    path = Path(path)
    return path.with_name(path.stem + ".fwd_window.json")


REPO_ROOT = Path(__file__).resolve().parents[1]
LOG_FILE = REPO_ROOT / "docs" / "experiments" / "LOG.md"
REGISTRATION_PREFIX = f"{PROTOCOL_ID} WINDOW REGISTERED:"
MAX_COMMIT_DELAY = timedelta(days=3)          # the registration line must be committed promptly
PRE_START_GRACE = timedelta(hours=1)          # shadow cycles of the registered code before start
PROTOCOL_FILES = ("research/fwd_protocol.py", "research/shadow_scoring.py", "research/shadow_quality.py",
                  "research/protocol.py", "backtest/costs.py")


def protocol_fingerprint() -> str:
    """Content hash of the evaluator, scorer, quality gates, protocol calendar and cost model:
    every rule that turns journal rows into a KEEP/REJECT. Pinned by the registration."""
    import hashlib

    digest = hashlib.sha256()
    for rel in PROTOCOL_FILES:
        digest.update(rel.encode("utf-8"))
        digest.update((REPO_ROOT / rel).read_bytes().replace(b"\r\n", b"\n"))
    return digest.hexdigest()[:16]


def registration_line(record: dict) -> str:
    """The exact line that must be COMMITTED to docs/experiments/LOG.md."""
    return (f"{REGISTRATION_PREFIX} start_utc={record['start_utc']} registered_at_utc={record['registered_at_utc']} "
            f"decision_fingerprint={record['decision_fingerprint']} config_hash={record['config_hash']} "
            f"protocol_fingerprint={record['protocol_fingerprint']}")


def committed_registration_lines(repo: Path | None = None) -> dict[str, datetime]:
    """Every distinct registration line EVER committed on any ref (git history is shared by all
    worktrees), with the time of the first commit that added it. Uncommitted edits do not count;
    a deleted line still counts (a re-registration is visible forever)."""
    import subprocess

    out = subprocess.run(
        ["git", "log", "--all", "--reverse", "--format=@@%cI", "-p", "-S", REGISTRATION_PREFIX, "--",
         "docs/experiments/LOG.md"],
        cwd=repo or REPO_ROOT, capture_output=True, text=True, timeout=30, check=False)
    lines: dict[str, datetime] = {}
    when = None
    for raw in (out.stdout or "").splitlines():
        if raw.startswith("@@") and not raw.startswith("@@ "):
            when = datetime.fromisoformat(raw[2:].strip())
        elif raw.startswith("+" + REGISTRATION_PREFIX) and when is not None:
            lines.setdefault(raw[1:].strip(), when)
    return lines


def register_window(path: str | Path, *, config_hash: str, start: datetime | None = None) -> dict:
    """Pin the evidence window ONCE (refuses to overwrite). Records the start, the decision-code
    fingerprint, the decision-config hash and the protocol fingerprint (evaluator + scorer +
    gates). The start may not be backdated."""
    from research.shadow_journal import decision_fingerprint

    if not config_hash:
        raise ValueError("config_hash is required")
    target = window_file(Path(path))
    if target.exists():
        raise FileExistsError(f"FWD window already registered: {target}")
    now = datetime.now(timezone.utc)
    begin = start or now
    if begin < now - timedelta(minutes=5):
        raise ValueError("the window start cannot be backdated")
    record = {"protocol": PROTOCOL_ID, "start_utc": begin.isoformat(), "registered_at_utc": now.isoformat(),
              "decision_fingerprint": decision_fingerprint(), "config_hash": config_hash,
              "protocol_fingerprint": protocol_fingerprint()}
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return record


def end_window(path: str | Path, reason: str) -> dict:
    """Append an END record (never deletes). An ended window is never binding; its interim
    state must be reported in LOG.md and it counts in the family (docs/FWD_PROTOCOL.md)."""
    target = window_file(Path(path))
    record = json.loads(target.read_text(encoding="utf-8"))
    if "ended_at_utc" not in record:
        record["ended_at_utc"] = datetime.now(timezone.utc).isoformat()
        record["end_reason"] = str(reason)
        target.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return record


def _registration(path: Path) -> dict | None:
    target = window_file(path)
    if not target.exists():
        return None
    try:
        record = json.loads(target.read_text(encoding="utf-8"))
    except ValueError:
        return {"corrupt": True}
    return record if isinstance(record, dict) else {"corrupt": True}


def _registration_refusal(path: Path, registration: dict) -> str | None:
    if registration.get("corrupt"):
        return "registration file unreadable"
    if "ended_at_utc" in registration:
        return f"window ended at {registration['ended_at_utc']} ({registration.get('end_reason')}): report it, never bind"
    required = ("start_utc", "registered_at_utc", "decision_fingerprint", "config_hash", "protocol_fingerprint")
    if any(not registration.get(k) for k in required):
        return "registration incomplete"
    line = registration_line(registration)
    committed = committed_registration_lines()
    if line not in committed:
        return "registration line not committed to docs/experiments/LOG.md"
    if len(committed) > 1:
        return (f"{len(committed)} {PROTOCOL_ID} registrations were committed: a new window needs a new "
                f"protocol id and counts in the family")
    registered_at = _utc(registration["registered_at_utc"])
    commit_time = committed[line].astimezone(timezone.utc)
    if not (registered_at - timedelta(minutes=1) <= commit_time <= registered_at + MAX_COMMIT_DELAY):
        return "registration line committed outside the allowed delay"
    if protocol_fingerprint() != registration["protocol_fingerprint"]:
        return "evaluator/scorer/gates changed since registration (run the evaluator from the pinned checkout)"
    start = _utc(registration["start_utc"])
    conn = sqlite3.connect(path)
    try:
        early = conn.execute(
            "SELECT COUNT(*) FROM discovery_cycles WHERE run_mode='shadow_only' AND decision_fingerprint=? "
            "AND created_at < ?", (registration["decision_fingerprint"], (start - PRE_START_GRACE).isoformat())
        ).fetchone()[0]
    finally:
        conn.close()
    if early:
        return f"{early} cycles of the registered code ran before the window start (pre-registration look)"
    return None


def _arm(rows, arm: str) -> list:
    """Arm membership from the DECISION table (same definitions as the scorer's evaluate_row):
    SELECTED = a chosen LONG signal with a trade-type action; COUNTERFACTUAL = a NO_TRADE whose
    best evaluation (top_*) is LONG with a stop and a target."""
    by_cycle: dict[str, int] = {}
    for r in rows:
        by_cycle[r["cycle_id"]] = by_cycle.get(r["cycle_id"], 0) + 1
    usable = [r for r in rows if by_cycle[r["cycle_id"]] >= MIN_CYCLE_ROWS]
    if arm == "SELECTED":
        return [r for r in usable if r["strategy"] is not None and r["d_side"] == "LONG"
                and str(r["action"] or "").startswith(TRADE_ACTION_PREFIXES)]
    return [r for r in usable if r["strategy"] is None and r["top_side"] == "LONG"
            and r["top_stop"] is not None and r["top_target"] is not None]


def _decision_minimums(rows, test: str) -> bool:
    """Sample minimums counted from the decision table (independent of scoring progress)."""
    selected = _arm(rows, "SELECTED")
    if test == "FWD1":
        return (len(selected) >= MIN_EVENTS and len({_day(r) for r in selected}) >= MIN_DAYS
                and len({r["symbol"] for r in selected}) >= MIN_SYMBOLS)
    counterfactual = _arm(rows, "COUNTERFACTUAL")
    paired = {_day(r) for r in selected} & {_day(r) for r in counterfactual}
    return len(selected) >= MIN_EVENTS and len(counterfactual) >= MIN_EVENTS and len(paired) >= MIN_PAIRED_DAYS


def _window(path: Path, run_mode: str, test: str) -> tuple[list, dict]:
    """Rows up to the binding cutoff, with the cutoff, the binding flag and the window verdict."""
    registration = _registration(path)
    if registration is None:
        return [], {"binding": False, "reason": "window not registered (run_shadow_only.py --register-fwd-window)"}
    refusal = _registration_refusal(path, registration)
    if refusal:
        return [], {"binding": False, "reason": refusal}
    start = _utc(registration["start_utc"])
    deadline_end = datetime.combine(EVIDENCE_DEADLINE + timedelta(days=1), datetime.min.time(), tzinfo=_ET)
    rows = [r for r in _population(path, run_mode)
            if _utc(r["cycle_created"]) is not None and start <= _utc(r["cycle_created"]) < deadline_end]
    if not rows:
        return [], {"window_start": start.isoformat(), "binding": False, "reason": "no data in the window"}
    # Per-cycle exclusions FIRST (they depend only on each cycle), then the minimums.
    _, full = shadow_quality.evaluate(path, since=start, until=deadline_end, run_mode=run_mode)
    excluded = set(full["excluded_cycles"])
    rows = [r for r in rows if r["cycle_id"] not in excluded]
    days = sorted({_day(r) for r in rows if _day(r) <= EVIDENCE_DEADLINE.isoformat()})
    cutoff_day, reached = None, False
    for day in days:
        if _decision_minimums([r for r in rows if _day(r) <= day], test):
            cutoff_day, reached = day, True
            break
    deadline_passed = datetime.now(_ET).date() > EVIDENCE_DEADLINE
    if cutoff_day is None:
        cutoff_day = EVIDENCE_DEADLINE.isoformat() if deadline_passed else (days[-1] if days else None)
    rows = [r for r in rows if cutoff_day is not None and _day(r) <= cutoff_day]
    unscored = sum(1 for r in rows if r["status"] is None or (r["status"] == "PENDING_DATA" and r["fwd_5"] is None))
    binding = (reached or deadline_passed) and unscored == 0
    window = {"window_start": start.isoformat(), "cutoff_day": cutoff_day, "minimums_reached": reached,
              "deadline_passed": deadline_passed, "unscored_rows_to_cutoff": unscored, "binding": binding,
              "registered_fingerprint": registration.get("decision_fingerprint")}
    if rows:
        # Verdict over EVERY cycle from the registered start to the last cycle of the cutoff day
        # (excluded cycles included: a zero-tolerance event anywhere invalidates the window).
        cycle_times = [_utc(r["cycle_created"]) for r in rows]
        _, session = shadow_quality.evaluate(path, since=start,
                                             until=max(cycle_times) + timedelta(microseconds=1), run_mode=run_mode)
        fingerprints = {r["decision_fingerprint"] for r in rows}
        reasons = list(session["reasons"])
        if fingerprints != {registration.get("decision_fingerprint")}:
            reasons.append("decision code differs from the registered fingerprint")
        if {r["config_hash"] for r in rows} != {registration.get("config_hash")}:
            reasons.append("decision config differs from the registered config hash")
        window.update(quality="VALID_FOR_RESEARCH" if not reasons else "INVALID_FOR_RESEARCH",
                      quality_reasons=reasons, config_hashes=session.get("config_hashes"))
    return rows, window


def _arm_missing(rows, arms) -> dict[str, float | None]:
    """Missing forward returns PER ARM (a total can hide a selective hole in one arm)."""
    return {arm: _missing_share(_arm(rows, arm)) for arm in arms}


def _blind(result: dict) -> dict:
    """Before the binding moment NO outcome statistic leaves the evaluator (no interim looks)."""
    if result["window"].get("binding"):
        return result
    for key in ("gross_excess", "net_excess", "difference", "missing_share", "arm_missing_share",
                "critical_t", "no_trade_reasons"):
        if key in result:
            result[key] = "BLINDED"
    result["sample"] = {k: v for k, v in result["sample"].items() if k != "daily_sd_pct"}
    return result


def _missing_share(rows) -> float | None:
    if not rows:
        return None
    missing = sum(1 for r in rows if r["fwd_5"] is None or r["status"] not in ("FINAL", "PENDING_DATA"))
    return missing / len(rows)


def _gate(window: dict, missing, enough: bool) -> str | None:
    if window.get("reason"):
        return window["reason"]
    if not window.get("binding"):
        if window.get("unscored_rows_to_cutoff"):
            return "monitoring only: scoring incomplete up to the cutoff"
        return "monitoring only: minimum sample not reached and deadline not passed"
    if window.get("quality") != "VALID_FOR_RESEARCH":
        return f"evidence window invalid: {window.get('quality_reasons')}"
    worst = max((v for v in (missing.values() if isinstance(missing, dict) else [missing]) if v is not None),
                default=None)
    if worst is not None and worst > MAX_MISSING_SHARE:
        return f"missing forward returns {worst:.1%} > {MAX_MISSING_SHARE:.0%} (worst arm)"
    if not enough:
        return "deadline reached without the minimum sample"
    return None


def evaluate_fwd1(path: str | Path, run_mode: str = "shadow_only") -> dict:
    rows, window = _window(Path(path), run_mode, "FWD1")
    events = _excess_events(rows, "SELECTED")
    daily = _daily(events)
    gross = _stats(list(daily.values()))
    net = _stats([x - ROUND_TRIP_COST_PCT for x in daily.values()])
    sample = {"events": len(events), "days": len(daily), "symbols": len({s for _, s, _ in events})}
    missing = _arm_missing(rows, ("SELECTED",))
    decision, reason = "INCONCLUSIVE", _gate(window, missing, _decision_minimums(rows, "FWD1"))
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
    return _blind({"protocol": PROTOCOL_ID, "test": "FWD1", "decision": decision, "reason": reason,
                   "sample": sample, "gross_excess": gross, "net_excess": net, "arm_missing_share": missing,
                   "window": window, "critical_t": critical_t(len(daily)) if daily else None})


def evaluate_fwd2(path: str | Path, run_mode: str = "shadow_only") -> dict:
    rows, window = _window(Path(path), run_mode, "FWD2")
    sel_events, cf_events = _excess_events(rows, "SELECTED"), _excess_events(rows, "COUNTERFACTUAL")
    sel, cf = _daily(sel_events), _daily(cf_events)
    paired = [sel[d] - cf[d] for d in sorted(set(sel) & set(cf))]
    diff = _stats(paired)
    missing = _arm_missing(rows, ("SELECTED", "COUNTERFACTUAL"))
    decision, reason = "INCONCLUSIVE", _gate(window, missing, _decision_minimums(rows, "FWD2"))
    if reason is None and diff["se_pct"] is not None and diff["t"] is not None:
        crit = critical_t(len(paired))
        half_width = t_quantile(0.975, len(paired) - 1) * diff["se_pct"]
        if diff["mean_pct"] > 0 and diff["t"] >= crit:
            decision, reason = "KEEP", ("setups taken beat the NO_TRADE setups passed on (in practice mostly the "
                                        "high-volatility pause; regime-confounded by construction)")
        elif abs(diff["mean_pct"]) + half_width < ROUND_TRIP_COST_PCT:
            decision, reason = "REJECT", ("95% interval of the difference within +/- round-trip cost: NO_TRADE "
                                          "passes on setups as good as the ones it takes (hypothesis supported)")
        else:
            reason = "neither threshold crossed"
    elif reason is None:
        reason = "statistics not computable"
    # Pre-registered DESCRIPTIVE breakdown (never tested): which NO_TRADE rule produced the arm.
    reasons: dict[str, int] = {}
    for r in _arm(rows, "COUNTERFACTUAL"):
        reasons[str(r["reason"])] = reasons.get(str(r["reason"]), 0) + 1
    return _blind({"protocol": PROTOCOL_ID, "test": "FWD2", "decision": decision, "reason": reason,
                   "sample": {"selected": len(sel_events), "counterfactual": len(cf_events),
                              "paired_days": len(paired), "daily_sd_pct": _sd(paired)},
                   "difference": diff, "arm_missing_share": missing, "no_trade_reasons": reasons,
                   "window": window, "critical_t": critical_t(len(paired)) if paired else None})
