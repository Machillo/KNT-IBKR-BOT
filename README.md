# KNT IBKR Bot — Paper Autonomous Core

Safety-first autonomous trading infrastructure for Interactive Brokers using Python and `ib_async`.

## Current checkpoint

This build is intentionally **Paper-first** and does not arm a production trading strategy yet. It is now structured as a long-running autonomous core rather than a collection of one-shot tests.

Implemented:

- Async TWS / IB Gateway connection with reconnect attempts
- Standard LIVE-port lock unless explicitly enabled
- Account snapshot and account-scoped broker reconciliation
- Persistent daily NetLiquidation baseline by account/date
- Sticky daily-loss lock across restarts
- Shared RiskManager gate for new exposure
- Configurable max trade risk, max position value, and daily-loss limits
- Kill Switch with DRY_RUN and ARMED modes
- Account-scoped cancellation + liquidation + broker-side flat verification
- Fail-closed supervisor loop that continuously rechecks daily equity
- Startup lock when unexpected positions/open orders exist
- Explicit Paper broker smoke test behind a flag
- Broker-confirmed `position=0 / open_orders=0` after the smoke round-trip
- IBKR scanner discovery adapter for US most-active stocks
- Read-only market intelligence pipeline: scanner -> quotes -> spread/liquidity filter -> ranking
- Strategy layer remains disabled until intelligence/risk behavior is Paper-validated

## Setup

```bash
python -m venv .venv
source .venv/Scripts/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env
pytest -q
python main.py
```

PowerShell activation:

```powershell
.\.venv\Scripts\Activate.ps1
```

## Safe default run

With the default flags, `python main.py`:

1. connects to Paper,
2. reads account state,
3. restores/creates the daily risk baseline,
4. reconciles broker positions/orders,
5. starts the continuous risk supervisor,
6. places **no strategy orders**.

The old SPY BUY/SELL smoke test no longer runs every startup. To run it intentionally:

```env
RUN_BROKER_SMOKE_TESTS=true
```

Return it to `false` after the test.

## Read-only discovery test

To test the first market-intelligence adapter without placing orders:

```env
RUN_DISCOVERY_PROBE=true
DISCOVERY_ROWS=10
```

This asks IBKR for a US-stock scanner result, requests quotes sequentially, rejects unusable/wide-spread names, and logs a ranking. It is market intelligence, **not** a trading strategy.

## Kill Switch

Development default:

```env
KILL_SWITCH_ENABLED=true
KILL_SWITCH_DRY_RUN=true
```

When the daily-loss condition is reached, the lock is persisted immediately. In DRY_RUN, the bot logs what it would cancel/flatten but does not send liquidation actions.

`KILL_SWITCH_DRY_RUN=false` arms broker actions and must only be enabled for a controlled Paper test. The armed path cancels working orders for the selected account, sends offsetting market orders for reported positions, and requires broker-side reconciliation to flat.

## Safety properties

- A restart cannot reset the same day's risk baseline.
- A sticky daily Kill Switch cannot be bypassed by creating a fresh RiskManager.
- New exposure goes through RiskGatedOrderManager.
- Risk-reducing exits use a separate path.
- Monitoring errors lock new entries fail-closed.
- Unexpected startup exposure can lock the session.
- LIVE standard ports remain blocked while `ALLOW_LIVE_TRADING=false`.

## What is intentionally not claimed yet

This checkpoint is **not a profitable autonomous strategy** and is **not production/live ready**. The current discovery adapter covers US stocks first. Forex, futures, options, strategy selection, portfolio-level exposure/risk models, asset-specific sizing/multipliers, and robust strategy research are subsequent intelligence/strategy phases.
