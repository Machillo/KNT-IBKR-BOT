"""Summarize a shadow session and run the data-quality gates (offline, read-only journal).

Reads the shadow-only journal by default (``state/shadow_only/strategy_performance.db``).
Prints a text summary and, with ``--json``, the full summary. ``--persist`` records the gate
results in ``research_quality`` (journal data is never modified or deleted), so a session
marked INVALID_FOR_RESEARCH keeps its data and its reasons.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def main() -> None:
    from config.config import state_path
    from research import shadow_quality
    from research.shadow_report import render, summarize

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=None, help="default: <repo>/state/shadow_only/strategy_performance.db")
    ap.add_argument("--since", default=None, help="ISO time (UTC if naive)")
    ap.add_argument("--until", default=None, help="ISO time (UTC if naive)")
    ap.add_argument("--run-mode", default="shadow_only", help="'any' for every run mode")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--persist", action="store_true", help="store gate results in research_quality")
    ap.add_argument("--register-fwd-window", action="store_true",
                    help="pin the FWD-v1 evidence window start (once; refuses to overwrite)")
    ap.add_argument("--fwd", action="store_true",
                    help="also run the pre-registered FWD1/FWD2 evaluator (binding only at the registered moment)")
    a = ap.parse_args()
    db = a.db or state_path("shadow_only", "strategy_performance.db")
    run_mode = None if a.run_mode == "any" else a.run_mode
    since, until = _parse(a.since), _parse(a.until)
    if a.register_fwd_window:
        from research.fwd_protocol import register_window
        record = register_window(db)
        print(f"FWD WINDOW registered | start={record['start_utc']} fingerprint={record['decision_fingerprint']}")
    summary = summarize(db, since=since, until=until, run_mode=run_mode)
    print(render(summary))
    if a.json:
        print(json.dumps(summary, indent=2, default=str))
    if a.fwd:
        from research.fwd_protocol import evaluate_fwd1, evaluate_fwd2
        for result in (evaluate_fwd1(db), evaluate_fwd2(db)):
            print(json.dumps(result, indent=2, default=str))
    if a.persist:
        results, session = shadow_quality.evaluate(db, since=since, until=until, run_mode=run_mode)
        shadow_quality.persist(db, results, session)
        print(f"QUALITY persisted | gates={len(results)} verdict={session['verdict']}")


if __name__ == "__main__":
    main()
