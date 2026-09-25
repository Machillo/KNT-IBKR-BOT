# Overnight progress — checkpoint (session 2, rounds 1–3)

Session 1 (`claude/overnight-hardening`, up to 868f7d9) was merged by Kenneth into
`feature/paper-alpha` via PR #2 (merge commit 0356044). Session 2 lives on
`claude/overnight-session2` (replayed onto `feature/paper-alpha`) with **draft PR #3** (base
`feature/paper-alpha`). Nothing merged into `main`. No orders, no IBKR connection, no paper run,
no kill-switch drill against IBKR in session 2; the HOLDOUT was never evaluated.

## Session 1 (see git log up to 868f7d9)
Paper guard, executor gates, backtester v2, leak-free validation, pipeline replay, protocol v1,
H1–H9 all REJECT, null model, shadow journal v1. Tests 124 → 275.

## Session 2, rounds 1–2 — blocks
| block | key commits |
|---|---|
| Point-in-time discovery funnel (every scanner row, status, reason, sources) + cycle metadata + decision context | 1ffd3c2 |
| Idempotent shadow scorer (TRADE + NO_TRADE counterfactual, MAE/MFE, fwd 1/5/20, dedup, alignment, LIMIT/DAY) | 85c0c36, a48148e, 6ce5c9a |
| Shared `DecisionPipeline` (runtime = replay); replay v3 with real allocator/admission, LIMIT/DAY entries, caps | 715a25a, 6bd561b |
| Point-in-time universe providers (journal, CSV with delistings) + replay wiring + journal bar fetcher | 715a25a, a03d021, 2631820 |
| Risk: sticky multi-day drawdown lock; sector concentration with fail-closed metadata; working orders count for correlation; in-cycle exposure update | c512d34, d1d626d, 6bd561b |
| Kill-switch paper drill runner + runbook (NOT run) | ac2003e |
| Order-incapable shadow-only runner for forward evidence | 57e7762 |
| Cohort-neutral event study + round 3 families F1–F6 (all REJECT) + stop condition + forward hypotheses | 9751343 … 677f985, cd587a5 |
| `market-discovery` skill; docs POINT_IN_TIME_DATA, REPLAY_PARITY, KILL_SWITCH_DRILL | 75c29f6, a03d021 |
| Safety review: drawdown failures never block the kill switch, readonly sessions refused by the guard, drill limits, ACKs refused from .env, shadow-only isolated state | 570706d, 53f5a1c, 1d7113d |
| Diagnostic: baseline + null model under replay v3 | 5e5d40a |

At the end of round 2: tests 275 → 352.

## Session 2, round 3 — forward-evidence readiness (commits from 620282a to the PR head)
| block | what changed | key commits |
|---|---|---|
| State safety | every store/runner defaults to repo-anchored `STATE_DIR`/`REPORTS_DIR`; repo-anchored `.env` for ACK refusals; per-test isolation + real-state guard | 620282a, f051864, 634c215 |
| Shadow-only cannot send orders | live settings / clientId ≤ 0 refused; readonly; forced dry-run kill switch; static scan + two full-cycle recording-IB tests (worst case; seeded cycle reaching SHADOW_SUBMIT); bot's persisted locks mirrored read-only | b01b1bf, 6fdafc7, 7dc8991 |
| Shared logic | `execution/pretrade.py` (session, lock, finite sanity, side, short, daily cap, decision-bar freshness, STK/whole shares, tick, geometry, fresh reference) + hard risk on rounded prices — executor, shadow-only and replay | bb93eaf, b3d556a, d20565b |
| Forward evidence (shadow-only) | frozen learning; SHADOW_SUBMIT / SHADOW_BLOCKED:<reason>; virtual book (restored after restart); journal v2 (sizing, sector, correlation, quote, session, bars, input hash, run/learning mode, candidate errors, cycle completion, scanner rows/errors, config hash, decision-code fingerprint) | f342577, 46d4693, ea87927, 6dbb833 |
| Scorer v3 | fills only for executable decisions; forward returns from the first tradeable price after the decision; DAY entries only in the decision's session; expiry only by IBKR; leave-one-out benchmark | c3a1764, 972f454, 6dbb833 |
| Quality gates + report | `run_shadow_report.py`; `INVALID_FOR_RESEARCH` with reasons, data kept; verdict on market-open cycles of one run mode; decision-code / config change invalidates | 91a1877, e813ae2, 6dbb833 |
| Replay v4 | shared 140-bar context, executor pretrade + hard risk, drawdown lock, `.env` limits, UTC→ET keys (look-ahead fix), liquidity order, warm-up, `runtime_equivalent` only for 1 h data with default knobs | 62dfb35, 8c3b7bd, 004e8a9, ea87927 |
| Risk interactions | state-write failure never stops the kill switch; correct kill reason; failure paths lock before journaling; persistent execution lock (human reset with ACK + read-only flatness check); pending orders count against the cash reserve; stale decision bars refused; drill sees every client's orders; sector metadata TTL; no balances in logs | aec284b, d737a69, ab3d5db, 8d33fed, 97d8b1f, a28c3f1 |
| Protocol & data | FWD-v1 (`docs/FWD_PROTOCOL.md`): registered window, binding cutoff from decisions, deadline cap, HH/Student-t, missing-data limit; holdout closed at 2026-09-25 + FORWARD segment; external dataset spec + strict PIT loaders | f4f5325, 4951491, 6dbb833, 247926a |
| Paper readiness (NOT run) | `run_paper_preflight.py` (read-only, incl. drawdown-state and execution-lock checks); one-shot runner: 3 confirmations, ≤ 1 share; `docs/PAPER_PLUMBING_TEST.md` with upgrade steps and rollback | c31dcbb, f8a7155 |

Reviews in round 3 (independent agents, read-only):
- execution-safety: 3 blockers → fixed; re-verification: 0 blockers, 6 should-fix → fixed (a28c3f1).
- quant-methodology: 1 + 1 blockers, 4 must-fix → fixed; re-verification: 2 blockers (cutoff) → fixed (6dbb833).
- trading-architect: 3 must-fix → fixed (ea87927).
- release-gate: 3 blockers (upgraded-install drawdown state, this stale doc, PR disclosures) → fixed (f8a7155, this file, PR body).

Tests: 352 → 470 passing (`python -m pytest -q`). Mutation evidence: `python tools/mutation_check.py`
(12/12 killed: stale bar, execution lock, restart backstop, journal-after-transmit lock, flatten
lock, lock-before-journal, merge by instant, scorer v3 base, FWD deadline cap, preflight drawdown
check, cash reserve, replay liquidity order), plus ad-hoc ones during the round (shadow-only
readonly/dry-run, injected cancel, supervisor shielding, UTC keys, virtual book + restore,
learning freeze, scorer executable gate / LOO, replay drawdown / pretrade, leakage gate).

## Research status
- Tests on protocol-v1 VALIDATION: **15** (H1–H9, F1–F6) → stop searching this dataset.
- HOLDOUT [2025-03-01, 2026-09-25): unused.
- No KEEP. F6: selector-logic LONG picks on daily/4h bars underperform the cohort after costs in
  VALIDATION (|t| ≈ 3.4–3.6); F1 is a survivorship artefact; replay-v3 positives match the null model.
- FWD1–FWD3 pre-registered; evaluation rules fixed in FWD-v1. No forward data exists yet.

## Next actions (human)
1. Review draft PR #3, including the behavior changes listed in its body. Do not merge unreviewed.
2. From a clean checkout of the reviewed commit: `MARKET_DATA_TYPE=1`, start `python run_shadow_only.py`
   during market hours (read-only), then `python run_shadow_report.py --register-fwd-window` once.
3. Daily/weekly: `python run_fetch_journal_bars.py`, `python run_score_shadow.py`,
   `python run_shadow_report.py --persist`.
4. After ≥ 10 VALID trading days and a dry-run drill: `python run_paper_preflight.py`, then the
   supervised 1-share plumbing test (`docs/PAPER_PLUMBING_TEST.md`).
5. Decide on an external survivorship-free dataset (`docs/POINT_IN_TIME_DATA.md`).
