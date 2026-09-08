from __future__ import annotations

from dataclasses import dataclass

from market.history import HistoricalDataService
from strategies.momentum import MomentumStrategy, SignalSide, StrategySignal
from utils.logger import logger


@dataclass(frozen=True)
class ShadowDecision:
    symbol: str
    liquidity_score: float
    signal: StrategySignal
    action: str


class ShadowTradingEngine:
    """Evaluates real discovered candidates but never sends an order."""

    def __init__(self, ib, market_intelligence, minimum_signal_score: float = 10.0) -> None:
        self.intelligence = market_intelligence
        self.history = HistoricalDataService(ib)
        self.strategy = MomentumStrategy()
        self.minimum_signal_score = minimum_signal_score

    async def run_once(self, rows: int = 10) -> list[ShadowDecision]:
        ranked = await self.intelligence.ranked_us_stocks(rows)
        decisions: list[ShadowDecision] = []
        for candidate in [x for x in ranked if x.eligible][:5]:
            try:
                bars = await self.history.bars(candidate.contract)
                signal = self.strategy.evaluate(bars)
                action = "NO_TRADE"
                if signal.side != SignalSide.FLAT and signal.score >= self.minimum_signal_score:
                    action = f"WOULD_{signal.side.value}"
                decision = ShadowDecision(candidate.symbol, candidate.score, signal, action)
                decisions.append(decision)
                logger.info(
                    "SHADOW DECISION | symbol=%s liquidity=%.2f strategy=%s side=%s signal_score=%.2f action=%s reason=%s entry=%s stop=%s target=%s",
                    candidate.symbol, candidate.score, self.strategy.name, signal.side.value,
                    signal.score, action, signal.reason, signal.entry, signal.stop, signal.target,
                )
            except Exception as exc:
                logger.warning("SHADOW candidate skipped | symbol=%s error=%s", candidate.symbol, exc)
        return decisions
