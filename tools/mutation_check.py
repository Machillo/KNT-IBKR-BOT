"""Mutation check for the safety/research regression tests (developer tool; no broker access).

Each entry removes one fix (a string mutation), runs the regression test that must catch it,
expects a FAILURE, then restores the file with ``git checkout``. Refuses to touch a file that
has uncommitted changes. Usage: ``python tools/mutation_check.py`` from the repo root.
"""
import subprocess
import sys
from pathlib import Path

MUTATIONS = [
    ("stale decision bar refused", "execution/pretrade.py",
     "        if age < 0 or age > ctx.max_bar_age_seconds:", "        if False:",
     "tests/test_paper_execution.py::test_paper_execution_refuses_stale_or_unknown_decision_bars"),
    ("persistent execution lock", "execution/paper.py",
     "        if self.execution_lock.read() is not None:", "        if False:",
     "tests/test_paper_execution.py::test_execution_lock_persists_across_days_until_a_human_clears_it"),
    ("unresolved failure after restart (journal backstop)", "execution/paper.py",
     "        if self.journal.unresolved_failures_on(today):", "        if False:",
     "tests/test_paper_execution.py::test_unresolved_failure_blocks_entries_after_a_restart"),
    ("journal failure after transmit locks", "execution/paper.py",
     '            self._lock(f"paper trade journal write failed after transmission ({status}); entries locked")',
     "            raise",
     "tests/test_paper_execution.py::test_journal_failure_after_transmit_still_confirms_and_locks"),
    ("flattened partial bracket locks", "execution/paper.py",
     '                self._lock(f"paper bracket {parent_id} partially filled and flattened; broker state must be checked")',
     "                pass",
     "tests/test_paper_execution.py::test_flattened_partial_bracket_locks_new_entries"),
    ("lock before journal on failure paths", "execution/paper.py",
     '            self._lock("paper execution transmission error; broker state must be checked")\n            self._record_safely(normalized, "FAILED", reason, None)',
     '            self.journal.record(normalized, status="FAILED", reason=reason)\n            self._lock("paper execution transmission error; broker state must be checked")',
     "tests/test_paper_execution.py::test_failure_paths_lock_even_when_the_journal_is_down"),
    ("merge bars by instant", "run_fetch_journal_bars.py",
     "    by_time = {key(r): r for r in existing}\n    by_time.update({key(r): r for r in fresh})",
     '    by_time = {str(r["time"]): r for r in existing}\n    by_time.update({str(r["time"]): r for r in fresh})',
     "tests/test_replay_parity.py::test_merge_bars_dedupes_the_same_instant_written_differently"),
    ("scorer v3 base = first tradeable price", "research/shadow_scoring.py",
     "    reference = float(bars_after[0].open) if bars_after and float(bars_after[0].open) > 0 else None",
     '    reference = float(row["reference_close"]) if row["reference_close"] is not None else None',
     "tests/test_shadow_scoring.py::test_forward_return_starts_at_the_first_tradeable_price_not_before_the_decision"),
    ("FWD cutoff capped at the deadline", "research/fwd_protocol.py",
     '    days = sorted({_day(r) for r in rows if _day(r) <= EVIDENCE_DEADLINE.isoformat()})',
     '    days = sorted({_day(r) for r in rows})',
     "tests/test_fwd_protocol.py::test_cutoff_never_lands_after_the_deadline"),
    ("preflight drawdown-init check", "execution/preflight.py",
     '        "drawdown_state_initialized", missing is False,', '        "drawdown_state_initialized", True,',
     "tests/test_paper_preflight.py::test_preflight_fails_when_the_drawdown_state_is_missing_for_an_account_with_history"),
    ("cash reserve counts pending", "portfolio/brain.py",
     "        projected_cash = snapshot.cash - snapshot.pending_order_notional - opportunity.proposed_notional",
     "        projected_cash = snapshot.cash - opportunity.proposed_notional",
     "tests/test_portfolio_brain.py::test_pending_entries_count_against_the_cash_reserve"),
    ("candidate liquidity order in replay", "research/pipeline_backtest.py",
     "            pending_decisions.sort(key=lambda x: (x[0], x[1]))",
     "            pending_decisions.sort(key=lambda x: (-x[4].selected.adjusted_score if x[4].selected else 1, x[1]))",
     "tests/test_replay_parity.py::test_capacity_goes_in_the_runtime_liquidity_order_not_by_score"),
    ("bracket protective children GTC", "core/order_manager.py",
     '        bracket.stopLoss.tif = "GTC"', '        bracket.stopLoss.tif = "DAY"',
     "tests/test_paper_execution.py::test_bracket_entry_is_day_but_protective_children_are_gtc"),
    ("kill switch never market-flattens non-stock", "risk/kill_switch.py",
     '            if sec_type != "STK" or qty != int(qty):', "            if False:",
     "tests/test_kill_switch_drill.py::test_kill_switch_never_market_flattens_non_stock_positions"),
    ("executor lifecycle gate", "execution/paper.py",
     "        if status not in self.allowed_strategy_statuses:", "        if False:",
     "tests/test_paper_execution.py::test_autonomous_executor_refuses_strategies_without_paper_status"),
    ("learning off by default", "engine/shadow.py",
     "                 learning_enabled: bool = False, research_execution: bool = False,",
     "                 learning_enabled: bool = True, research_execution: bool = False,",
     "tests/test_shadow_research_path.py::test_learning_is_off_by_default_for_every_runtime_mode"),
    ("selector evaluates only selectable strategies", "engine/strategy_selector.py",
     "                           if status_of(s.name) in SELECTABLE]", "                           ]",
     "tests/test_paper_execution.py::test_selector_never_evaluates_research_or_retired_strategies"),
    ("FWD window restart guard", "run_shadow_only.py",
     '    if record.get("decision_fingerprint") != decision_fingerprint():', "    if False:",
     "tests/test_shadow_only.py::test_shadow_only_refuses_to_restart_on_code_or_config_that_breaks_a_registered_window"),
    ("hard risk default 1%", "config/config.py",
     'getenv("MAX_TRADE_RISK_PCT", "0.01")', 'getenv("MAX_TRADE_RISK_PCT", "0.10")',
     "tests/test_replay_parity.py::test_pipeline_sizes_on_tick_rounded_prices_and_hard_risk_defaults_to_one_percent"),
]

ok = True
for name, path, old, new, test in MUTATIONS:
    p = Path(path)
    if subprocess.run(["git", "diff", "--quiet", "--", path]).returncode != 0:
        print(f"SKIP  | {name} | {path} has uncommitted changes")
        ok = False
        continue
    text = p.read_text(encoding="utf-8")
    if old not in text:
        print(f"SKIP  | {name} | pattern not found")
        ok = False
        continue
    p.write_text(text.replace(old, new, 1), encoding="utf-8")
    try:
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", test], capture_output=True, text=True)
        verdict = "KILLED" if r.returncode != 0 else "SURVIVED"
        ok &= verdict == "KILLED"
        print(f"{verdict} | {name} | {test.split('::')[1]}")
    finally:
        subprocess.run(["git", "checkout", "--", path], check=True)
print("ALL MUTATIONS KILLED" if ok else "SOME MUTATIONS NOT KILLED")
sys.exit(0 if ok else 1)
