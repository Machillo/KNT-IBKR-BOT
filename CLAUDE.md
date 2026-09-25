# KNT-IBKR-BOT — project rules for Claude

Always-loaded rules. Deeper guidance lives in `.claude/skills/` (§7); independent
reviewers live in `.claude/agents/` (§8). The current code is the source of truth:
inspect it before assuming behavior.

## 1. What this is

Research + trading system on Interactive Brokers (`ib_async`, TWS/IB Gateway).
Target flow: discovery → market intelligence → regime → strategy selection / NO_TRADE
→ portfolio allocation → hard risk → execution → measurement → research.
Promotion ladder: BACKTEST → OOS/WALK-FORWARD → SHADOW → IBKR PAPER → SMALL LIVE → SCALE.
Today: US stocks only (STK). No FX/futures/options execution exists; do not add
asset classes without their own contract, sizing, session and risk model.

Layout: `config/` env config · `core/` IBKR connection, account, orders, **paper guard** ·
`market/` discovery, ranking, regime, history, session · `strategies/` rule strategies ·
`engine/` selector, shadow loop, supervisor · `portfolio/` sizing and admission ·
`risk/` risk manager, daily guard, kill switch, order gate · `execution/` paper executor ·
`backtest/` simulator, costs, metrics, validation · `research/` walk-forward, learning,
promotions, sweeps · `run_*.py` runners · `tests/`.

## 2. Absolute safety rules

1. **No live trading.** Never enable it, never weaken `ALLOW_LIVE_TRADING`, never add a path
   that could reach a live account. Live requires a future explicit human decision.
2. **No order is sent unless the session is verified PAPER** by `core/paper_guard.py`
   (account prefix + real socket port + config), re-verified per order. Port alone proves nothing.
   Every `placeOrder` goes through `OrderManager._transmit` or the guard; never call
   `ib.placeOrder` elsewhere.
3. Every entry passes the risk pipeline: allocator → `PortfolioAdmissionCoordinator` →
   `PaperExecutionEngine` (which re-applies `RiskManager.evaluate_trade`). No strategy or
   runner may bypass it.
4. Do not relax risk limits, kill switch, daily-loss lock, entry caps, ACKs or paper checks —
   and never change a limit to improve a metric. Tightening needs a stated reason.
5. Autonomous paper orders need `AUTONOMOUS_TRADING_ENABLED=true` **and** the literal
   `AUTONOMOUS_PAPER_ACK`. One-shot runners need their own literal ACK. Do not run order-capable
   runners unless the human asked for that specific run.
6. When unsure whether something is read-only or paper-only: **don't run it** (fail closed).
7. Never commit or print: `.env`, account IDs, `state/`, `reports/`, logs, `*.db`, balances,
   positions. The repository is **public**. Mask accounts with `mask_account()` in logs.

## 3. Quantitative honesty

- The 5 %/month figure is a research ambition, never a constraint to satisfy.
- No look-ahead, leakage, survivorship shortcuts, cherry-picking, or selecting on the test set.
- Protocol: chronological TRAIN → VALIDATION → FINAL HOLDOUT. The holdout is never used to choose
  strategy, parameters, symbols, contexts or promotions. Once used for a decision it is no longer a
  holdout; start a new cycle with a new time split.
- Every performance claim states: data span, split, costs model, trades, return, max drawdown,
  and whether it is in-sample. Report negative results plainly. CI green ≠ edge.
- Operational learning (selector bonuses, promotions) may only use admissible OOS evidence
  (see `strategy-validation` skill).

## 4. Git

- Default: one new branch per task from the agreed base; PRs never merge themselves.
- Temporary base: `feature/paper-alpha` (unmerged PR #1) holds the real project; `main` is an
  initial commit. Work branches from it until the human merges PR #1. Never merge to `main`.
- Stage files by name; never `git add -A`. Small, descriptive commits. No force-push.

## 5. Validation

- `python -m pytest -q` (all offline, IBKR mocked). Start focused, then module, then full suite.
- Offline research uses `reports/history_cache` only (`research/` + `run_*` cache runners).
- Report separately: validated automatically · needs IBKR paper/manual validation · not validated.
- Report pre-existing failures separately from regressions.

## 6. Working state

Durable state lives in Git, tests and `docs/`. The current checkpoint and next action are in
`docs/OVERNIGHT_PROGRESS.md` — read it first when resuming.

## 7. Skills (`.claude/skills/`)

| Skill | Use for |
|---|---|
| `knt-trading-core` | Any KNT work: architecture, flow, conventions, where things live |
| `ibkr-execution-safety` | Anything touching orders, IBKR connection, risk, kill switch, runners |
| `strategy-validation` | Backtests, walk-forward, metrics, costs, research claims, learning |
| `paper-to-live-gate` | Deciding readiness for shadow/paper/live; promotion criteria |
| `context-efficiency` | Long sessions, audits, research loops — save context without losing rigor |
| `market-discovery` | Scanners, universe, liquidity ranking, point-in-time journaling, research universes without hindsight |

## 8. Agents (`.claude/agents/`)

- `trading-architect` — read-only investigation of flows and design.
- `execution-safety-auditor` — adversarial review of any change to orders/risk/IBKR/runners.
- `quant-methodology-auditor` — adversarial review of backtest/validation changes and any performance claim.
- `release-gate-reviewer` — branch/PR preflight and ladder-stage readiness.

The implementer never reviews its own work as "independent". If a host cannot load project agents,
run a general-purpose agent with the agent file's instructions and say so. Agents have no shell:
give them the diff.
