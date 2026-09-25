"""Only APPROVED decisions ever reach PaperExecutionEngine.submit (fake executor, no IBKR)."""
import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from config.config import RiskConfig, read_only_ibkr_settings, IBKRConfig
from market.history import PriceBar
from portfolio.brain import PortfolioSnapshot
from portfolio.state import PortfolioState, PositionExposure
from risk.risk_manager import RiskManager
from strategies.momentum import SignalSide, StrategySignal

T0 = datetime(2026, 1, 5, 10)


def bars(n=120):
    out, price = [], 100.0
    for i in range(n):
        price *= 1.002 + 0.002 * (((i * 7) % 5) - 2) / 2  # deterministic noise, positive drift
        out.append(PriceBar(T0 + timedelta(hours=i), price * 0.999, price * 1.003, price * 0.997, price, 1e6))
    return out


class Fixed:
    name = "fixed_v1"
    warmup = 30

    def __init__(self, side):
        self.side = side

    def evaluate(self, b):
        p = b[-1].close
        if self.side == SignalSide.FLAT:
            return StrategySignal(SignalSide.FLAT, 0, None, None, None, "flat")
        if self.side == SignalSide.LONG:
            return StrategySignal(self.side, 99, p, p * 0.98, p * 1.04, "fixed")
        return StrategySignal(self.side, 99, p, p * 1.02, p * 0.96, "fixed")


class RecordingExecutor:
    allow_short = False

    def __init__(self):
        self.calls = []

    async def submit(self, contract, request):
        self.calls.append(request)
        return SimpleNamespace(submitted=True, reason="fake")


def state(net_liq=100_000.0, locked=False, positions=()):
    committed = sum(p.notional for p in positions)
    return PortfolioState(PortfolioSnapshot(net_liq, net_liq - committed, committed, 0, 0, 0, net_liq * 0.1, locked),
                          tuple(positions), ())


# An open position whose returns are perfectly correlated with the candidate: the
# allocator DOES produce a proposal, and admission must reject it (correlation limit).
CORRELATED = (PositionExposure("B", "STK", 10, 100.0, 1_000.0, con_id=2),)


def run_shadow(tmp_path, monkeypatch, side, portfolio_state, risk=True, industry="Technology", n_candidates=1):
    monkeypatch.chdir(tmp_path)
    from engine.shadow import ShadowTradingEngine

    candidates = [SimpleNamespace(symbol=sym, score=90.0, eligible=True,
                                  contract=SimpleNamespace(secType="STK", conId=cid, symbol=sym))
                  for sym, cid in (("A", 1), ("B", 2))[:n_candidates]]

    async def ranked(rows_per_plan, quote_budget):
        return candidates

    async def snap(contract, symbol, timeout=3.0):
        return SimpleNamespace(bid=100.0, ask=100.02, last=100.01, market_price=100.01, market_data_type=1)

    intel = SimpleNamespace(ranked_us_opportunity_universe=ranked, last_funnel=[], universe=None,
                            market_data=SimpleNamespace(snapshot_contract=snap, settings=SimpleNamespace(market_data_type=1)))
    executor = RecordingExecutor()
    async def details(contract):
        return [SimpleNamespace(industry=industry, category="Semiconductors", stockType="COMMON")]

    shadow = ShadowTradingEngine(SimpleNamespace(reqContractDetailsAsync=details), intel, research_budget=0,
                                 risk_manager=RiskManager(RiskConfig()) if risk else None,
                                 paper_executor=executor)
    shadow.selector.strategies = [Fixed(side)]
    shadow.selector.pause_directional_high_volatility = False

    async def fake_bars(contract, **kwargs):
        return bars()

    shadow.history.bars = fake_bars
    shadow.session_policy = SimpleNamespace(state=lambda now=None: SimpleNamespace(
        market_open=True, session="REGULAR", local_time=datetime(2026, 1, 5, 11)))
    decisions = asyncio.run(shadow.run_once(5, portfolio_state=portfolio_state))
    return decisions, executor


@pytest.mark.parametrize("side,portfolio,risk,expected", [
    (SignalSide.FLAT, state(), True, "NO_TRADE"),
    (SignalSide.SHORT, state(), True, "NO_TRADE"),                  # shorts refused before sizing
    (SignalSide.LONG, None, True, "WOULD_LONG"),                    # no portfolio state -> never submit
    (SignalSide.LONG, state(), False, "PORTFOLIO_REJECTED"),        # no hard risk manager
    (SignalSide.LONG, state(locked=True), True, "PORTFOLIO_REJECTED"),
    (SignalSide.LONG, state(net_liq=50.0), True, "PORTFOLIO_REJECTED"),  # cannot size one share
    (SignalSide.LONG, state(positions=CORRELATED), True, "PORTFOLIO_REJECTED"),  # proposal exists, admission says no
])
def test_unapproved_setups_never_reach_the_executor(tmp_path, monkeypatch, side, portfolio, risk, expected):
    decisions, executor = run_shadow(tmp_path, monkeypatch, side, portfolio, risk)
    assert executor.calls == []
    assert decisions[0].action == expected


def test_approved_long_reaches_executor_with_admitted_size(tmp_path, monkeypatch):
    decisions, executor = run_shadow(tmp_path, monkeypatch, SignalSide.LONG, state(), True)
    assert decisions[0].action == "PAPER_SUBMITTED"
    assert len(executor.calls) == 1 and executor.calls[0].quantity >= 1
    assert executor.calls[0].market_data_type == 1 and executor.calls[0].account_equity == 100_000.0


def test_read_only_settings_force_readonly_and_separate_client_id():
    base = IBKRConfig(port=7497, client_id=901, readonly=False)
    ro = read_only_ibkr_settings(base, 50)
    assert ro.readonly is True and ro.client_id == 951 and ro.port == 7497


def test_missing_sector_metadata_blocks_submission(tmp_path, monkeypatch):
    decisions, executor = run_shadow(tmp_path, monkeypatch, SignalSide.LONG, state(), True, industry="")
    assert executor.calls == []
    assert decisions[0].action == "PORTFOLIO_REJECTED" and decisions[0].portfolio_reason == "sector_metadata_missing"


def test_sector_concentration_limit_applies_with_metadata():
    from portfolio.admission import PortfolioAdmissionCoordinator
    from portfolio.state import PositionExposure

    coordinator = PortfolioAdmissionCoordinator(RiskManager(RiskConfig()), require_sector_metadata=True)
    held = tuple(PositionExposure(f"T{i}", "STK", 100, 100.0, 10_000.0, con_id=i + 1) for i in range(3))
    st = state(positions=held)
    kwargs = dict(state=st, symbol="NEW", asset_class="STK", side="LONG", quantity=10, entry_price=100.0,
                  stop_price=95.0, proposed_notional=1_000.0, proposed_risk=50.0, candidate_returns=(),
                  position_returns={p.symbol: (0.01, -0.01) * 30 for p in held}, correlation_to_portfolio=0.1)
    same = coordinator.evaluate(sector="Technology", sectors={p.symbol: "Technology" for p in held}, **kwargs)
    other = coordinator.evaluate(sector="Energy", sectors={p.symbol: "Technology" for p in held}, **kwargs)
    missing = coordinator.evaluate(sector="Energy", sectors={"T0": "Technology"}, **kwargs)
    assert same.reason == "same_sector_exposure_limit"
    assert other.approved
    assert missing.reason == "sector_metadata_missing"


def test_entry_submitted_earlier_in_the_cycle_counts_for_later_candidates(tmp_path, monkeypatch):
    # A and B have identical bars (correlation 1). Once A is submitted, B must be rejected
    # in the same cycle instead of being checked against the stale start-of-cycle state.
    decisions, executor = run_shadow(tmp_path, monkeypatch, SignalSide.LONG, state(), True, n_candidates=2)
    assert [d.action for d in decisions] == ["PAPER_SUBMITTED", "PORTFOLIO_REJECTED"]
    assert decisions[1].portfolio_reason == "correlation_limit"
    assert len(executor.calls) == 1
