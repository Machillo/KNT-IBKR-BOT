# First supervised paper run — plan

**Purpose: plumbing test only.** It verifies the order path (paper guard → risk re-check →
fresh quote → bracket → journal → broker reconciliation). It says NOTHING about strategy edge:
no validated candidate exists (docs/experiments/LOG.md). Live trading is out of scope.

## Preconditions (human)
1. Paper login in TWS or IB Gateway; account is flat (no positions, no open orders).
2. Local `.env` (never committed):
   - `IBKR_PORT=7497` (TWS paper) or `4002` (Gateway paper), matching the running app;
   - `IBKR_ACCOUNT=<your DU account>` (required if the login lists more than one account);
   - `ALLOW_LIVE_TRADING=false`, `IBKR_READONLY=false`;
   - `AUTONOMOUS_TRADING_ENABLED=false` (the one-shot runner refuses otherwise);
   - `REQUIRE_FLAT_STARTUP=true`, `KILL_SWITCH_ENABLED=true`, `KILL_SWITCH_DRY_RUN=true`;
   - `MARKET_DATA_TYPE=1` — **entries require live quotes**. The paper account must have live
     US equity data (shared from the live account's subscriptions). With 3/4 every entry is refused.
3. Run `python -m pytest -q` first (expect all passing).
4. Regular US session, not in the first 5 / last 15 minutes, not a holiday.

## Step A — bracket plumbing (no fill expected)
```bash
PAPER_SMOKE_ACK=I_UNDERSTAND_THIS_SUBMITS_A_PAPER_ORDER python run_paper_bracket_smoke.py
```
Places a 1-share BUY bracket at 50 % of market and cancels it. Expect: `PAPER BRACKET ACCEPTED`,
then cleanup with all three legs cancelled, zero fills. If anything fills: stop and inspect.

## Step B — one strategy-originated signal, max 1 share
```bash
KNT_SIGNAL_PAPER_ACK=I_UNDERSTAND_KNT_WILL_SUBMIT_ONE_PAPER_SIGNAL KNT_SIGNAL_PAPER_MAX_QTY=1 python run_knt_signal_paper_once.py
```
Expect either `no strategy setup passed every gate` (valid outcome) or exactly one bracket.
Log line `PAPER VERIFICATION | verified=True reason=verified_paper account=DU***xx` must appear;
if `verified=False`, the run must place nothing — that is the guard working.

## Who / how long / how to stop
- One person watches TWS and the console for the whole run (≈ 5 minutes per step).
- Stop: Ctrl-C the script; in TWS cancel any working order ("Cancel All") and flatten manually
  if a position exists. The bot never needs to be trusted to clean up.

## After the run — check and record
- TWS: at most one bracket (parent + take-profit + stop), quantity ≤ 1.
- `state/strategy_performance.db`, table `paper_trade_journal`: INTENT → PENDING → SUBMITTED
  (or a BLOCKED/REJECTED row with a reason).
- `logs/bot.log`: account shown masked (`DU***xx`), no full account id.
- Broker state matches the log (`KNT SIGNAL PAPER BROKER STATE`).
- Record the outcome (date, steps run, result, anomalies) in `docs/OVERNIGHT_PROGRESS.md` or a
  new run log — without account ids or balances.

## Not in this run
Autonomous loop (`paper_alpha.py` with ACK), armed kill switch, shorts, any size above 1 share.
Each needs its own plan after this plumbing test passes.
