# KNT IBKR Bot — Paper Alpha

Safety-first autonomous trading research bot using Python, `ib_async`, TWS/IB Gateway and Interactive Brokers Paper Trading.

## Current architecture

`IBKR discovery -> market data/liquidity -> historical bars -> regime detection -> strategy library -> selector -> shadow decision -> risk -> execution`

The strategy layer never sends orders directly. Autonomous strategy execution remains disabled by default.

## Strategy library

The initial selector evaluates seven single-asset families plus one two-asset family:

1. Breakout (`breakout_v1`)
2. Momentum + Gap (`momentum_gap_v1`)
3. Swing / Market Structure (`swing_structure_v1`)
4. Trend Following (`trend_following_v1`)
5. Mean Reversion (`mean_reversion_v1`)
6. Range Trading (`range_v1`)
7. Quantified SMC / Liquidity Sweeps (`smc_liquidity_v1`)
8. Pairs / Market Neutral (`pairs_market_neutral_v1`)

`momentum_v1` remains in the repository as the original baseline used to validate the backtest engine; it is not part of the eight-family selector.

## Market universe

KNT does **not** use a permanent ticker whitelist. Discovery is scanner-plan driven through IBKR. A scanner plan describes a broker market segment (`instrument`, `locationCode`, `scanCode`); strategies do not know or care which ticker list produced the candidate.

The currently validated runtime adapter is `STK / STK.US.MAJOR / MOST_ACTIVE`. Additional IBKR scanner segments can plug into the same discovery service via `ScannerPlan`/`scan_many` without changing strategy code. Asset-specific risk/execution models must be added before autonomous order execution is allowed for derivatives/FX.

## Safe Paper configuration

```env
IBKR_HOST=127.0.0.1
IBKR_PORT=7497
IBKR_CLIENT_ID=901
IBKR_READONLY=false
ALLOW_LIVE_TRADING=false
MAX_TRADE_RISK_PCT=0.10
MAX_DAILY_LOSS_PCT=0.10
MAX_POSITION_PCT=0.10
KILL_SWITCH_ENABLED=true
KILL_SWITCH_DRY_RUN=true
MARKET_DATA_TYPE=3
SUPERVISOR_POLL_SECONDS=15
REQUIRE_FLAT_STARTUP=true
RUN_BROKER_SMOKE_TESTS=false
RUN_DISCOVERY_PROBE=false
DISCOVERY_ROWS=10
AUTONOMOUS_TRADING_ENABLED=false
SHADOW_TRADING_ENABLED=true
SHADOW_INTERVAL_SECONDS=900
```

## Local validation

```bash
git fetch origin
git checkout feature/paper-alpha
git pull
pytest -q
python run_strategy_suite.py SPY --duration "180 D" --bar-size "1 hour"
python paper_alpha.py
```

The suite runner prints return, max drawdown, trades, win rate, profit factor and Sharpe for every single-asset family plus the original momentum baseline.

The shadow process discovers candidates dynamically, detects regime, evaluates all applicable strategies, selects the strongest setup or `NO_TRADE`, evaluates pair opportunities and sends no strategy orders.

Look for `SHADOW DECISION`, `SHADOW SETUP` and `SHADOW PAIR` in the log.

## Risk / production status

- Paper only during this phase.
- `ALLOW_LIVE_TRADING=false` blocks standard live ports.
- Persistent daily-loss state and sticky kill switch are retained.
- Kill-switch liquidation remains dry-run by default until explicitly Paper-tested.
- Cross-asset discovery is scanner-plan ready, but autonomous execution must use the correct contract multiplier/currency/options risk model for each asset class.
- No strategy currently has a claim of durable edge. The next stages are broad backtesting, out-of-sample/walk-forward validation, persistent strategy performance evidence, portfolio risk and controlled Paper execution.
