# Kill-switch paper drill — runbook (human-run, supervised)

Goal: prove the ARMED kill switch cancels working orders and flattens positions on a verified
PAPER account, and does nothing on anything else. Until this drill passes, keep
`KILL_SWITCH_DRY_RUN=true` in `.env`.

## Preconditions
- Paper login; `.env`: paper port, `IBKR_ACCOUNT=<DU account>`, `ALLOW_LIVE_TRADING=false`.
- The account holds **only** a position you opened by hand for the drill: 1 share of a liquid
  stock (e.g. buy 1 share in TWS). Optionally leave one far-from-market limit order working.
- Regular US session, somebody watching TWS.

## Steps
1. Dry run (sends nothing):
   ```bash
   python run_kill_switch_paper_drill.py
   ```
   Expect `ran=True armed=False`; the log shows `KILL SWITCH DRY-RUN ... would_cancel=N would_flatten=1`.
   If it prints `positions_exceed_drill_limits` or `paper_unverified:*`, stop and fix the setup.
2. Armed drill:
   ```bash
   KILL_SWITCH_DRILL_ACK=I_AM_WATCHING_AND_ACCEPT_THE_PAPER_KILL_SWITCH_DRILL python run_kill_switch_paper_drill.py --armed --max-qty 1
   ```
   Expect: working orders cancelled, one market SELL of 1 share, `flat_confirmed=True`,
   log `KILL SWITCH COMPLETE`.

## Verify afterwards
- TWS: no positions, no working orders.
- Log: account masked (`DU***xx`); `KILL SWITCH FLATTEN requested` exactly once.
- If `flat_confirmed=False`: flatten manually in TWS and record what happened.

## Guarantees built into the runner (tested in `tests/test_kill_switch_drill.py`)
- Refuses unless the session is verified PAPER (account prefix + real port + config).
- Refuses if any position is not a stock or exceeds `--max-qty` shares.
- Armed mode needs both `--armed` and the literal ACK; otherwise it only reports.
- Every cancel/liquidation goes through the paper guard, re-verified per order.

## After a successful drill
Record date and result in `docs/OVERNIGHT_PROGRESS.md` (no account ids or balances). Changing
`KILL_SWITCH_DRY_RUN` to `false` for autonomous paper runs is a separate human decision.
