"""Data-quality gates for forward shadow evidence.

Every gate reads the journal only; nothing is ever deleted or modified. Results go to
``research_quality`` (one row per scope/ref/gate/version), so a bad cycle or session stays in
the database with the reason it is ``INVALID_FOR_RESEARCH``.

Severities:
* BLOCKING — the cycle/decision must be excluded from research;
* WARNING  — usable, but the limitation must be reported with any result;
* INFO     — context.

A session (an explicit time window of ONE run mode) is ``INVALID_FOR_RESEARCH`` when a
ZERO-TOLERANCE gate fires in a market-open cycle (future leakage, non-determinism on identical
inputs, learning drift, a transmit-marked decision in a closed session or on a stale bar), when
the code or decision configuration changed inside the window (several code versions, a dirty
tree, several config hashes), or when more than ``MAX_BLOCKING_CYCLE_SHARE`` of its MARKET-OPEN
cycles carry a BLOCKING gate. Closed-market cycles are gated and recorded but never decide the
verdict: FWD evidence only uses open-market cycles. Thresholds: docs/FWD_PROTOCOL.md.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backtest.metrics import as_datetime
from engine.decision import DECISION_CONTEXT_BARS
from research.shadow_journal import DECISION_VERSION, ShadowJournal

GATES_VERSION = "g1"
MAX_BLOCKING_CYCLE_SHARE = 0.05
BAR_SECONDS = 3600
# pretrade refuses bars older than 75 min at ITS clock; created_at is written a moment later,
# so the gate allows a small margin before calling a transmit "stale".
MAX_DECISION_LAG = timedelta(minutes=80)
INCOMPLETE_GRACE = timedelta(hours=1)        # a cycle younger than this may still be running
ZERO_TOLERANCE = frozenset({"future_leakage", "nondeterministic_decision", "learning_drift",
                            "transmit_in_closed_session", "transmit_on_stale_bar"})
SESSION_INVALIDATORS = frozenset({"decision_code_changed", "decision_code_unknown", "config_changed",
                                  "config_unknown"})
TRANSMIT_ACTIONS = ("SHADOW_SUBMIT", "PAPER_SUBMITTED")


@dataclass(frozen=True)
class GateResult:
    scope: str        # cycle | decision | session
    ref: str
    gate: str
    severity: str     # BLOCKING | WARNING | INFO
    detail: str


def _utc(value) -> datetime | None:
    dt = as_datetime(value)
    if dt is None:
        return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(timezone.utc)


def _load(path: Path, since: datetime | None, until: datetime | None, run_mode: str | None):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    with conn:
        cycles = conn.execute("SELECT * FROM discovery_cycles ORDER BY created_at").fetchall()
        decisions = conn.execute("SELECT * FROM shadow_decisions ORDER BY id").fetchall()
        funnel = conn.execute("SELECT cycle_id, symbol, con_id, status, sources FROM discovery_funnel").fetchall()
    conn.close()

    def in_window(created) -> bool:
        t = _utc(created)
        return t is not None and (since is None or t >= since) and (until is None or t < until)

    # Strict run mode: rows without one (legacy, dev smoke runs) never belong to a mode's window.
    keep = {c["cycle_id"] for c in cycles if in_window(c["created_at"])
            and (run_mode is None or c["run_mode"] == run_mode)}
    cycles = [c for c in cycles if c["cycle_id"] in keep]
    decisions = [d for d in decisions if d["cycle_id"] in keep
                 and (run_mode is None or d["run_mode"] == run_mode)]
    funnel = [f for f in funnel if f["cycle_id"] in keep]
    return cycles, decisions, funnel


def evaluate(path: str | Path, *, since: datetime | None = None, until: datetime | None = None,
             run_mode: str | None = "shadow_only", now: datetime | None = None) -> tuple[list[GateResult], dict]:
    path = Path(path)
    ShadowJournal(path)  # schema (append-only migrations) exists
    now = now or datetime.now(timezone.utc)
    cycles, decisions, funnel = _load(path, since, until, run_mode)
    results: list[GateResult] = []
    add = results.append

    by_cycle_decisions: dict[str, list] = {}
    for d in decisions:
        by_cycle_decisions.setdefault(d["cycle_id"], []).append(d)
    by_cycle_funnel: dict[str, list] = {}
    for f in funnel:
        by_cycle_funnel.setdefault(f["cycle_id"], []).append(f)

    for c in cycles:
        cid = c["cycle_id"]
        created = _utc(c["created_at"])
        # G1 complete cycle
        if c["cycle_end"] is None:
            if created is not None and now - created > INCOMPLETE_GRACE:
                add(GateResult("cycle", cid, "incomplete_cycle", "BLOCKING", "no cycle_end marker"))
        else:
            journaled = len(by_cycle_decisions.get(cid, []))
            attempted = c["candidates_attempted"] or 0
            if journaled != attempted:
                add(GateResult("cycle", cid, "decisions_missing", "BLOCKING",
                               f"attempted={attempted} journaled={journaled}"))
            if (c["candidate_errors"] or 0) > 0:
                add(GateResult("cycle", cid, "candidate_errors", "WARNING", f"errors={c['candidate_errors']}"))
            eligible, cap = c["eligible_count"] or 0, c["max_candidates"] or 0
            if cap and eligible > cap:
                add(GateResult("cycle", cid, "deep_analysis_cap", "INFO", f"eligible={eligible} analysed<={cap}"))
        # Version / learning
        if c["decision_version"] not in (None, DECISION_VERSION):
            add(GateResult("cycle", cid, "legacy_decision_version", "BLOCKING", str(c["decision_version"])))
        if c["learning_mode"] not in (None, "frozen"):
            add(GateResult("cycle", cid, "learning_drift", "BLOCKING", f"learning_mode={c['learning_mode']}"))
        # G2 scanners
        planned = json.loads(c["scanners"] or "[]")
        rows = json.loads(c["scanner_rows"]) if c["scanner_rows"] else None
        errors = json.loads(c["scanner_errors"]) if c["scanner_errors"] else {}
        for name, err in sorted(errors.items()):
            add(GateResult("cycle", cid, "scanner_failed", "BLOCKING", f"{name}:{err}"))
        if rows is not None:
            for name in planned:
                if name not in errors and rows.get(name, 0) == 0:
                    add(GateResult("cycle", cid, "scanner_empty", "BLOCKING", name))
            per = c["rows_per_scanner"]
            full = sorted(n for n, k in rows.items() if per and k >= per)
            if full:
                add(GateResult("cycle", cid, "scanner_truncated", "WARNING",
                               f"top-{per} per scanner: {','.join(full)}"))
        elif c["cycle_end"] is not None:
            add(GateResult("cycle", cid, "scanner_rows_unknown", "WARNING", "journal predates scanner accounting"))
        # G3 quote budget
        statuses = [f["status"] for f in by_cycle_funnel.get(cid, [])]
        if statuses and statuses.count("not_quoted_budget"):
            add(GateResult("cycle", cid, "quote_budget_truncated", "WARNING",
                           f"not_quoted={statuses.count('not_quoted_budget')} of {len(statuses)}"))
        if statuses and statuses.count("quote_error") > len(statuses) / 2:
            add(GateResult("cycle", cid, "quote_errors_majority", "BLOCKING",
                           f"quote_error={statuses.count('quote_error')} of {len(statuses)}"))
        # G4 live data / G9 session known
        if c["market_data_type"] not in (1, None):
            add(GateResult("cycle", cid, "not_live_market_data", "BLOCKING", f"type={c['market_data_type']}"))
        if c["market_open"] is None:
            add(GateResult("cycle", cid, "session_unknown", "BLOCKING", "market_open not recorded"))
        # G8 missing conId in the eligible funnel
        missing = sum(1 for f in by_cycle_funnel.get(cid, []) if f["status"] == "ranked_eligible" and not f["con_id"])
        if missing:
            add(GateResult("cycle", cid, "eligible_missing_conid", "BLOCKING", f"n={missing}"))

    for d in decisions:
        did = str(d["id"])
        action = str(d["action"] or "")
        if action == "CANDIDATE_ERROR":
            continue  # accounted for at cycle level
        created, bar = _utc(d["created_at"]), _utc(d["bar_time"])
        # G5 clock / G11 leakage (canonical rows only: duplicates are re-decisions by design)
        if d["bar_time"] is not None and bar is None:
            add(GateResult("decision", did, "naive_bar_time", "BLOCKING", str(d["bar_time"])))
        if bar is not None and created is not None and d["duplicate_of"] is None:
            available = bar + timedelta(seconds=BAR_SECONDS)
            if created < available:
                add(GateResult("decision", did, "future_leakage", "BLOCKING",
                               f"decided {created.isoformat()} before bar complete {available.isoformat()}"))
            elif action in TRANSMIT_ACTIONS and created - available > MAX_DECISION_LAG:
                # pretrade refuses stale decision bars; a transmit-marked stale row is a bug.
                add(GateResult("decision", did, "transmit_on_stale_bar", "BLOCKING",
                               f"lag={(created - available).total_seconds() / 60:.0f}min"))
        # G7 determinism
        if d["conflict_with"] is not None:
            add(GateResult("decision", did, "nondeterministic_decision", "BLOCKING",
                           f"conflicts with #{d['conflict_with']}"))
        # G8 identifiers / sector
        if not d["con_id"]:
            add(GateResult("decision", did, "missing_conid", "BLOCKING", d["symbol"]))
        if action in TRANSMIT_ACTIONS and not d["sector"]:
            add(GateResult("decision", did, "missing_sector", "BLOCKING", d["symbol"]))
        # G6 history depth
        if d["bar_count"] is not None and d["bar_count"] < DECISION_CONTEXT_BARS:
            add(GateResult("decision", did, "short_history", "INFO", f"bars={d['bar_count']}"))
        # G9 session
        if action in TRANSMIT_ACTIONS and d["session_open"] == 0:
            add(GateResult("decision", did, "transmit_in_closed_session", "BLOCKING", action))
        # G12 learning frozen
        if d["learning_mode"] not in (None, "frozen") or (d["selector_bonus"] or 0) != 0:
            add(GateResult("decision", did, "learning_drift", "BLOCKING",
                           f"mode={d['learning_mode']} bonus={d['selector_bonus']}"))
        if d["decision_version"] != DECISION_VERSION:
            add(GateResult("decision", did, "legacy_decision_version", "BLOCKING", str(d["decision_version"])))

    # Invalidation: the code and the decision configuration must be constant inside the window.
    # Informational: git commits (docs/scorer commits change HEAD without changing decisions).
    versions = sorted({str(c["code_version"]) for c in cycles if c["code_version"]})
    fingerprints = sorted({str(c["decision_fingerprint"]) for c in cycles if c["cycle_end"] and c["decision_fingerprint"]})
    if len(fingerprints) > 1:
        add(GateResult("session", "window", "decision_code_changed", "BLOCKING", ",".join(fingerprints)))
    if run_mode == "shadow_only" and any(c["cycle_end"] and not c["decision_fingerprint"] for c in cycles):
        add(GateResult("session", "window", "decision_code_unknown", "BLOCKING", "cycles without a fingerprint"))
    configs = sorted({str(c["config_hash"]) for c in cycles if c["cycle_end"] and c["config_hash"]})
    if len(configs) > 1:
        add(GateResult("session", "window", "config_changed", "BLOCKING", ",".join(configs)))
    if run_mode == "shadow_only" and any(c["cycle_end"] and not c["config_hash"] for c in cycles):
        add(GateResult("session", "window", "config_unknown", "BLOCKING", "cycles without config_hash"))

    open_cycles = {c["cycle_id"] for c in cycles if c["market_open"] == 1}
    blocking_cycles = {r.ref for r in results if r.scope == "cycle" and r.severity == "BLOCKING"}
    decision_cycle = {str(d["id"]): d["cycle_id"] for d in decisions}
    blocking_cycles |= {decision_cycle[r.ref] for r in results
                        if r.scope == "decision" and r.severity == "BLOCKING" and r.ref in decision_cycle}
    blocking_open = blocking_cycles & open_cycles
    share = len(blocking_open) / len(open_cycles) if open_cycles else None

    def cycle_of(r: GateResult) -> str | None:
        return r.ref if r.scope == "cycle" else decision_cycle.get(r.ref)

    zero_tolerance = sorted({r.gate for r in results if r.gate in ZERO_TOLERANCE and cycle_of(r) in open_cycles})
    invalidators = sorted({r.gate for r in results if r.gate in SESSION_INVALIDATORS})
    reasons = []
    if not open_cycles:
        reasons.append("no_market_open_cycles_in_window")
    if zero_tolerance:
        reasons.append("zero_tolerance:" + ",".join(zero_tolerance))
    if invalidators:
        reasons.append("invalidated:" + ",".join(invalidators))
    if share is not None and share > MAX_BLOCKING_CYCLE_SHARE:
        reasons.append(f"blocking_open_cycle_share={share:.1%}>{MAX_BLOCKING_CYCLE_SHARE:.0%}")
    verdict = "INVALID_FOR_RESEARCH" if reasons else "VALID_FOR_RESEARCH"
    session = {
        "gates_version": GATES_VERSION, "decision_version": DECISION_VERSION, "run_mode": run_mode,
        "since": since.isoformat() if since else None, "until": until.isoformat() if until else None,
        "cycles": len(cycles), "market_open_cycles": len(open_cycles), "decisions": len(decisions),
        "cycles_with_blocking_gate": len(blocking_cycles), "open_cycles_with_blocking_gate": len(blocking_open),
        "blocking_open_cycle_share": share, "verdict": verdict, "reasons": reasons,
        "code_versions": versions, "decision_fingerprints": fingerprints, "config_hashes": configs,
        "excluded_cycles": sorted(blocking_cycles),
    }
    return results, session


def persist(path: str | Path, results: list[GateResult], session: dict) -> None:
    """Upsert gate results (never touches journal data). Re-running is idempotent."""
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(Path(path)) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS research_quality (
                   scope TEXT NOT NULL, ref TEXT NOT NULL, gate TEXT NOT NULL, gates_version TEXT NOT NULL,
                   severity TEXT NOT NULL, detail TEXT, evaluated_at TEXT NOT NULL,
                   PRIMARY KEY (scope, ref, gate, gates_version))""")
        rows = [(r.scope, r.ref, r.gate, GATES_VERSION, r.severity, r.detail, now) for r in results]
        session_ref = f"{session['run_mode']}|{session['since']}|{session['until']}"
        rows.append(("session", session_ref, "verdict", GATES_VERSION,
                     "BLOCKING" if session["verdict"] == "INVALID_FOR_RESEARCH" else "INFO",
                     json.dumps({"verdict": session["verdict"], "reasons": session["reasons"]}), now))
        conn.executemany(
            """INSERT INTO research_quality (scope, ref, gate, gates_version, severity, detail, evaluated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(scope, ref, gate, gates_version) DO UPDATE SET
                 severity=excluded.severity, detail=excluded.detail, evaluated_at=excluded.evaluated_at""",
            rows)
