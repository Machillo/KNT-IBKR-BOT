"""Summary of a shadow session, computed ONLY from the journal (no estimates, no fills invented).

Every number is a count or a value read from ``discovery_cycles``, ``discovery_funnel``,
``shadow_decisions`` and ``shadow_outcomes``. A metric that cannot be computed from what was
recorded is ``None`` — never a default.
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path

from research import shadow_quality
from research.shadow_journal import ShadowJournal
from research.shadow_scoring import SCORER_VERSION, ShadowScorer

TRADE_ACTIONS = ("SHADOW_SUBMIT", "PAPER_SUBMITTED")


def _window_filter(rows, since, until):
    out = []
    for r in rows:
        t = shadow_quality._utc(r["created_at"])
        if t is None or (since is not None and t < since) or (until is not None and t >= until):
            continue
        out.append(r)
    return out


def summarize(path: str | Path, *, since: datetime | None = None, until: datetime | None = None,
              run_mode: str | None = "shadow_only") -> dict:
    path = Path(path)
    ShadowJournal(path)
    ShadowScorer(path)  # outcome table exists (read-only use below)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    with conn:
        cycles = _window_filter(conn.execute("SELECT * FROM discovery_cycles").fetchall(), since, until)
        if run_mode is not None:
            cycles = [c for c in cycles if c["run_mode"] in (run_mode, None)]
        ids = {c["cycle_id"] for c in cycles}
        funnel = [f for f in conn.execute("SELECT * FROM discovery_funnel").fetchall() if f["cycle_id"] in ids]
        decisions = [d for d in conn.execute("SELECT * FROM shadow_decisions ORDER BY id").fetchall()
                     if d["cycle_id"] in ids and (run_mode is None or d["run_mode"] in (run_mode, None))]
        decision_ids = {d["id"] for d in decisions}
        outcomes = [o for o in conn.execute("SELECT * FROM shadow_outcomes WHERE scorer_version=?",
                                            (SCORER_VERSION,)).fetchall() if o["decision_id"] in decision_ids]
    conn.close()

    canonical = [d for d in decisions if d["duplicate_of"] is None and d["action"] != "CANDIDATE_ERROR"]
    actions = Counter(str(d["action"]) for d in canonical)
    selected = [d for d in canonical if d["strategy"] is not None]
    no_trade = [d for d in canonical if d["action"] == "NO_TRADE"]
    blocked = [d for d in canonical if str(d["action"]).endswith("_BLOCKED")]
    rejected = [d for d in canonical if d["action"] == "PORTFOLIO_REJECTED"]
    funnel_status = Counter(str(f["status"]) for f in funnel)
    quoted = [f for f in funnel if f["status"] in ("ranked_eligible", "ranked_rejected")]

    results, quality = shadow_quality.evaluate(path, since=since, until=until, run_mode=run_mode)
    gates = Counter(f"{r.severity}:{r.gate}" for r in results)

    return {
        "window": {"since": since.isoformat() if since else None, "until": until.isoformat() if until else None,
                   "run_mode": run_mode,
                   "first_cycle": min((c["created_at"] for c in cycles), default=None),
                   "last_cycle": max((c["created_at"] for c in cycles), default=None)},
        "cycles": {
            "total": len(cycles),
            "complete": sum(1 for c in cycles if c["cycle_end"]),
            "market_open": sum(1 for c in cycles if c["market_open"] == 1),
            "market_closed": sum(1 for c in cycles if c["market_open"] == 0),
            "candidate_errors": sum(int(c["candidate_errors"] or 0) for c in cycles),
            "code_versions": sorted({c["code_version"] for c in cycles if c["code_version"]}),
            "decision_versions": sorted({c["decision_version"] for c in cycles if c["decision_version"]}),
            "learning_modes": sorted({c["learning_mode"] for c in cycles if c["learning_mode"]}),
        },
        "discovery": {
            "symbols_discovered": len({f["symbol"] for f in funnel}),
            "symbols_quoted": len({f["symbol"] for f in quoted}),
            "symbols_eligible": len({f["symbol"] for f in funnel if f["status"] == "ranked_eligible"}),
            "symbols_analysed": len({d["symbol"] for d in decisions}),
            "funnel_status": dict(sorted(funnel_status.items())),
            "ranked_rejection_reasons": dict(Counter(str(f["reason"]) for f in funnel
                                                     if f["status"] == "ranked_rejected").most_common(15)),
        },
        "decisions": {
            "journaled": len(decisions),
            "canonical": len(canonical),
            "duplicates": sum(1 for d in decisions if d["duplicate_of"] is not None),
            "conflicts": sum(1 for d in decisions if d["conflict_with"] is not None),
            "candidate_errors": sum(1 for d in decisions if d["action"] == "CANDIDATE_ERROR"),
            "by_action": dict(sorted(actions.items())),
            "trade": sum(actions[a] for a in TRADE_ACTIONS),
            "no_trade": len(no_trade),
            "no_trade_reasons": dict(Counter(str(d["reason"]) for d in no_trade).most_common(15)),
            "execution_blocks": dict(Counter(str(d["reason"]) for d in blocked).most_common(15)),
            "risk_blocks": dict(Counter(str(d["reason"]) for d in rejected).most_common(15)),
            "selected_strategy": dict(Counter(str(d["strategy"]) for d in selected).most_common()),
            "regime": dict(Counter(str(d["regime"]) for d in canonical).most_common()),
            "side": dict(Counter(str(d["side"]) for d in selected).most_common()),
        },
        "missing_data": {
            "decisions_without_conid": sum(1 for d in canonical if not d["con_id"]),
            "trade_rows_without_sector": sum(1 for d in canonical if d["action"] in TRADE_ACTIONS and not d["sector"]),
            "approved_without_reference_quote": sum(
                1 for d in canonical if str(d["action"]).startswith("SHADOW_") and d["reference_price"] is None),
            "reference_not_live": sum(
                1 for d in canonical if d["reference_data_type"] not in (None, 1)),
            "short_history_decisions": sum(
                1 for d in canonical if d["bar_count"] is not None and d["bar_count"] < 140),
        },
        "outcomes": {
            "scorer_version": SCORER_VERSION,
            "by_status": dict(sorted(Counter(str(o["status"]) for o in outcomes).items())),
            "unscored": len(canonical) - sum(1 for o in outcomes),
            "providers": sorted({o["provider"] for o in outcomes if o["provider"]}),
            "executable_final": sum(1 for o in outcomes if o["status"] == "FINAL" and o["executable"]),
            "note": "performance statistics: run_score_shadow.py (only FINAL executable rows carry fills)",
        },
        "quality": {**quality, "gates": dict(sorted(gates.items())),
                    "excluded_cycles": len(quality["excluded_cycles"])},
    }


def render(summary: dict) -> str:
    q, c, d = summary["quality"], summary["cycles"], summary["decisions"]
    lines = [
        f"SHADOW SESSION | mode={summary['window']['run_mode']} "
        f"{summary['window']['first_cycle']} .. {summary['window']['last_cycle']}",
        f"QUALITY | {q['verdict']} reasons={q['reasons']} cycles_with_blocking={q['cycles_with_blocking_gate']}",
        f"CYCLES | total={c['total']} complete={c['complete']} open={c['market_open']} closed={c['market_closed']} "
        f"errors={c['candidate_errors']} versions={c['decision_versions']} learning={c['learning_modes']}",
        f"DISCOVERY | {json.dumps(summary['discovery']['funnel_status'])} analysed={summary['discovery']['symbols_analysed']}",
        f"DECISIONS | canonical={d['canonical']} trade={d['trade']} no_trade={d['no_trade']} "
        f"duplicates={d['duplicates']} conflicts={d['conflicts']} by_action={json.dumps(d['by_action'])}",
        f"BLOCKS | execution={json.dumps(d['execution_blocks'])} risk={json.dumps(d['risk_blocks'])}",
        f"MISSING | {json.dumps(summary['missing_data'])}",
        f"OUTCOMES | {json.dumps(summary['outcomes']['by_status'])} unscored={summary['outcomes']['unscored']}",
    ]
    return "\n".join(lines)
