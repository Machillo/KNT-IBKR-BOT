# Supervised PAPER plumbing test: procedure (prepared, NOT executed)

**What it proves:** one strategy-originated bracket of at most 1 share goes correctly through
the whole order path: paper guard, pre-trade checks, hard risk, bracket, journal and broker
reconciliation.

**What it does not prove:** anything about edge. No validated strategy exists
(docs/experiments/LOG.md, docs/FWD_PROTOCOL.md). Live trading is out of scope.

**Prerequisites** (docs/FWD_PROTOCOL.md, "Shadow → Paper plumbing criteria"):
- ≥ 10 trading days of shadow-only on the v2 code, VALID_FOR_RESEARCH;
- block reasons understood;
- the kill-switch drill run in dry-run.

## Roles and time
- One human at the console and in TWS for the whole run, about 15 minutes.
- Regular US session, after the first 5 minutes and outside the last 15 (the session
  policy enforces this).

## Step 0: local configuration (`.env`, never committed)
- `IBKR_PORT=7497` (TWS paper) or `4002` (Gateway paper).
- `IBKR_ACCOUNT=<DU…>` if the login lists more than one account.
- `ALLOW_LIVE_TRADING=false`, `IBKR_READONLY=false`, `AUTONOMOUS_TRADING_ENABLED=false`.
- `REQUIRE_FLAT_STARTUP=true`, `KILL_SWITCH_ENABLED=true`, `KILL_SWITCH_DRY_RUN=true`.
- `MARKET_DATA_TYPE=1`. The paper account needs live US equity data shared from the live
  account.
- **No ACK variable in `.env`.** Every ACK is refused if it is persisted there.

## Step 1: tests
```bash
python -m pytest -q
```
Everything must pass.

## Step 2: read-only preflight (sends nothing)
```bash
python run_paper_preflight.py
```
It uses a read-only session and its own clientId. Every line must be `PASS`, and the final line
must be `PREFLIGHT | READY_FOR_SUPERVISED_PLUMBING`. The checks are:
- configuration: paper port, live trading off, kill switch enabled, live market data, flat
  startup, autonomous mode off, no persisted ACK;
- account: verifiable as PAPER (all managed accounts DU/DF, real socket port = paper port),
  NetLiquidation available;
- flatness: flat account, **no open orders from ANY clientId** (`reqAllOpenOrders`);
- data: a live two-sided quote (type 1) on SPY;
- session: open (WARN only; the test itself refuses a closed session);
- locks: no persisted sticky kill or drawdown lock in `state/`.

Any FAIL means stop, fix the cause and rerun. Never bypass a check.

## Step 3: kill switch available
- The supervisor inside the runner evaluates the daily-loss guard at startup. With
  `KILL_SWITCH_DRY_RUN=true` it would only report.
- The human kill switch for this test is **TWS "Cancel All" plus a manual flatten**. It does
  not depend on KNT.
- Optional before the test (dry-run, sends nothing):
  ```bash
  python run_kill_switch_paper_drill.py
  ```
  It must report `ran=True armed=False`.

## Step 4: the plumbing run (three confirmations; at most 1 bracket, at most 1 share)
```bash
KNT_SIGNAL_PAPER_ACK=I_UNDERSTAND_KNT_WILL_SUBMIT_ONE_PAPER_SIGNAL KNT_SIGNAL_PAPER_MAX_QTY=1 python run_knt_signal_paper_once.py --confirm-paper-plumbing
```
The runner refuses unless ALL of the following hold:
- the ACK is present for this run only;
- the flag is given;
- the in-process preflight passes (same broker checks as step 2);
- the paper guard verifies the session;
- the account is flat;
- the session is open;
- the quantity cap is ≤ 1.

Valid outcomes:
- `no strategy setup passed every gate`: nothing sent. This is a valid result, not a failure.
- One bracket (parent LMT + take-profit + stop) of 1 share.
- A fail-closed refusal with a reason, e.g. `sector_metadata_missing`, `correlation_limit`,
  `stale_decision_bar`, `reference_not_live_market_data`. Nothing is sent.

## Step 5: verification (record the result without account ids or balances)
- **TWS:** at most one bracket, quantity ≤ 1, account DU…
- **Journal** `state/strategy_performance.db` → `paper_trade_journal`: INTENT → PENDING →
  SUBMITTED, or one REJECTED/BLOCKED row with its reason.
- **Log:** `PAPER VERIFICATION | verified=True reason=verified_paper account=DU***xx`,
  `KNT PAPER PLUMBING PREFLIGHT | READY…`, and `KNT SIGNAL PAPER BROKER STATE` matching TWS.
- **Where to record it:** `docs/OVERNIGHT_PROGRESS.md` or a run log (date, outcome,
  anomalies).

## Rollback (in this order; none of it needs KNT to behave)
1. Ctrl-C the runner.
2. TWS: **Cancel All** working orders for the account.
3. If a position exists: flatten it manually in TWS (1 share). The supervised drill
   (`docs/KILL_SWITCH_DRILL.md`, armed, max 5 shares) is an alternative only if its own
   preconditions hold.
4. Check TWS: 0 positions, 0 working orders. Rerun `python run_paper_preflight.py`: flatness
   checks PASS.
5. If anything unexpected happened: leave the configuration unchanged, keep the logs and
   journal, and record the anomaly before any new run.

## Not part of this test
- The autonomous loop (`paper_alpha.py`).
- An armed kill switch.
- Shorts.
- Size above 1 share.
- More than one bracket.

Each needs its own plan after this plumbing test passes.
