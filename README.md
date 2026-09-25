# KNT IBKR Bot — Paper Alpha

Safety-first autonomous trading research bot using Python, `ib_async`, TWS/IB Gateway and
Interactive Brokers **Paper** Trading. Live trading is not supported.

Rules for contributors and Claude sessions: `CLAUDE.md`. Current checkpoint:
`docs/OVERNIGHT_PROGRESS.md`. Research log: `docs/experiments/LOG.md`.

## Architecture

`IBKR discovery (scanners) → liquidity ranking → completed historical bars → regime →
strategy selector (or NO_TRADE) → allocator → portfolio admission → paper executor`

Strategies never send orders. Every order goes through `core/paper_guard.py` and
`OrderManager._transmit`.

## Paper safety model

An order can only be transmitted when ALL of these hold (re-checked per order):

- `ALLOW_LIVE_TRADING=false` and `IBKR_PORT` is a paper port (7497 TWS / 4002 Gateway);
- the socket actually connected is that port;
- every account in `managedAccounts()` has an IBKR paper prefix (`DU`/`DF`), and the
  target account is unambiguous (set `IBKR_ACCOUNT` if the login has several);
- the supervisor is not locked (daily loss, non-flat startup, monitoring error);
- the executor's own checks pass: session open per IBKR's liquid-hours calendar, live
  (type 1) two-sided quote within 1.5 % of the entry, `RiskManager` limits using the
  broker's NetLiquidation, whole shares, long only, daily entry cap, no duplicate.

The autonomous loop (`paper_alpha.py`) submits only with `AUTONOMOUS_TRADING_ENABLED=true`
**and** `AUTONOMOUS_PAPER_ACK=I_UNDERSTAND_KNT_WILL_SUBMIT_AUTONOMOUS_PAPER_ORDERS` exported in
the shell for that session (an ACK stored in `.env` is refused).

## Configuration

See `.env.example`. Never commit `.env`, `state/`, `reports/`, logs or databases — the
repository is public and `.gitignore` enforces this.

## Validation

```bash
python -m pytest -q
```

Offline research on cached history (`reports/history_cache`, no IBKR connection):

```bash
python run_experiments.py --yearly                 # pre-registered pipeline variants, TRAIN+VALIDATION
python run_pipeline_backtest.py --profile long_10y --segment validation
```

Research protocol v1 (`research/protocol.py`): TRAIN < 2023-09-01 ≤ VALIDATION < 2025-03-01 ≤
HOLDOUT. The holdout is single-use per frozen candidate and requires `--confirm-holdout`.
15 hypotheses have been tested on that VALIDATION set (all rejected); new research needs new
data — see `docs/experiments/LOG.md` and `docs/POINT_IN_TIME_DATA.md`.

## Forward evidence (point-in-time, no orders)

```bash
python run_shadow_only.py            # read-only session, no executor; journals discovery + every decision
python run_score_shadow.py --source ibkr    # score TRADE and NO_TRADE decisions once bars exist
python run_fetch_journal_bars.py     # bars for every name the scanners surfaced (incl. later delistings)
```

Replaying the journal universe (`run_pipeline_backtest.py --universe files --cache-dir reports/pit_cache
--universe-source journal`) requires a protocol v2 time split written down BEFORE looking at
results; forward data must not be reinterpreted through the v1 holdout flags.

## Supervised human procedures
- First paper plumbing test: `docs/PAPER_RUN_PLAN.md`.
- Kill-switch paper drill: `docs/KILL_SWITCH_DRILL.md`.
- Multi-day drawdown lock (`MAX_DRAWDOWN_PCT`, default 15 %, sticky): reset only with
  `run_reset_drawdown_lock.py` and its literal ACK.
- Runtime vs replay differences: `docs/REPLAY_PARITY.md`.

## Status

- Paper only. No live path exists.
- Kill-switch liquidation remains dry-run by default until its armed flow is paper-tested.
- No strategy has demonstrated durable edge; see `docs/experiments/LOG.md` for evidence.
