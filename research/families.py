"""Round-3 signal families (pre-registered in docs/experiments/LOG.md).

Every signal has the event-study signature ``signal(symbol, bars_so_far, ctx) -> +1/0`` and
only reads ``bars_so_far`` (bars up to and including the current bar) or cross-sectional
values computed by ``ctx.cross_section`` from bars up to the same timestamp. Per-symbol
indicator series are precomputed causally (value at k uses bars[0..k] only) and cached.
"""
from __future__ import annotations

from math import ceil

from engine.strategy_selector import StrategySelector
from strategies.momentum import SignalSide

INDEX_ETFS = frozenset({"SPY", "QQQ", "IWM", "DIA"})


def _in_quantile(values: dict[str, float], symbol: str, fraction: float, top: bool) -> bool:
    if symbol not in values or len(values) < 5:
        return False
    ordered = sorted(values, key=lambda s: values[s], reverse=top)
    return symbol in ordered[:max(1, ceil(len(ordered) * fraction))]


def f1_xs_momentum(symbol, bars, ctx) -> int:
    k = len(bars) - 1
    if k < 253:
        return 0
    t_now, t_prev = str(bars[-1].time)[:7], str(bars[-2].time)[:7]
    if t_now == t_prev:  # only on the first bar of a new month
        return 0

    def mom(b, i):
        if i < 252 or b[i - 252].close <= 0:
            return None
        return b[i - 21].close / b[i - 252].close - 1

    values = ctx.cross_section(_stamp(bars[-1]), "mom_12_1", mom)
    return 1 if _in_quantile(values, symbol, 0.20, top=True) else 0


def f2_short_term_reversal(symbol, bars, ctx) -> int:
    if len(bars) < 6:
        return 0

    def r5(b, i):
        return None if i < 5 or b[i - 5].close <= 0 else b[i].close / b[i - 5].close - 1

    values = ctx.cross_section(_stamp(bars[-1]), "r5", r5)
    return 1 if _in_quantile(values, symbol, 0.20, top=False) else 0


class _VolatilityCompressionBreakout:
    """F3 with per-symbol causal indicator cache."""

    def __init__(self) -> None:
        self.cache: dict[str, list[bool]] = {}

    def _series(self, symbol: str, full) -> list[bool]:
        n = len(full)
        tr = [0.0] * n
        for i in range(1, n):
            c, p = full[i], full[i - 1]
            tr[i] = max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close))
        ratio: list[float | None] = [None] * n
        for i in range(51, n):
            a5 = sum(tr[i - 4:i + 1]) / 5
            a50 = sum(tr[i - 49:i + 1]) / 50
            ratio[i] = a5 / a50 if a50 > 0 else None
        compressed = [False] * n
        for i in range(300, n):
            window = [r for r in ratio[i - 249:i + 1] if r is not None]
            if len(window) < 200 or ratio[i] is None:
                continue
            rank = sum(r <= ratio[i] for r in window) / len(window)
            compressed[i] = rank <= 0.10
        signal = [False] * n
        for i in range(301, n):
            if not any(compressed[i - 4:i + 1]):
                continue
            prior_high = max(b.high for b in full[i - 20:i])
            avg_vol = sum(b.volume for b in full[i - 20:i]) / 20
            signal[i] = full[i].close > prior_high and avg_vol > 0 and full[i].volume > 1.5 * avg_vol
        return signal

    def __call__(self, symbol, bars, ctx) -> int:
        if symbol not in self.cache:
            self.cache[symbol] = self._series(symbol, ctx.data[symbol])
        k = len(bars) - 1
        series = self.cache[symbol]
        return 1 if k < len(series) and series[k] else 0


def f4_gap_down_reversal(symbol, bars, ctx) -> int:
    if len(bars) < 2 or bars[-2].close <= 0:
        return 0
    gap = bars[-1].open / bars[-2].close - 1
    return 1 if gap <= -0.03 and bars[-1].close < bars[-2].close else 0


def f5_residual_reversal(symbol, bars, ctx) -> int:
    if symbol in INDEX_ETFS or len(bars) < 6 or "SPY" not in ctx.data:
        return 0
    t = _stamp(bars[-1])
    spy = ctx.bars_through("SPY", t)
    if spy is None or len(spy) < 6 or spy[-6].close <= 0:
        return 0
    spy_r5 = spy[-1].close / spy[-6].close - 1

    def resid(b, i):
        if i < 5 or b[i - 5].close <= 0:
            return None
        return b[i].close / b[i - 5].close - 1 - spy_r5

    values = {s: v for s, v in ctx.cross_section(t, f"resid5", resid).items() if s not in INDEX_ETFS}
    return 1 if _in_quantile(values, symbol, 0.10, top=False) else 0


class _SelectorSignal:
    """F6: the live selector (all strategies, threshold 55, no learning) picks LONG."""

    def __init__(self) -> None:
        self.selector = StrategySelector(55.0, performance_store=None)

    def __call__(self, symbol, bars, ctx) -> int:
        if len(bars) < 60:
            return 0
        selection = self.selector.evaluate(bars[-450:], symbol=symbol, timeframe="")
        chosen = selection.selected
        return 1 if chosen is not None and chosen.signal.side == SignalSide.LONG else 0


def _stamp(bar):
    from research.event_study import _stamp as stamp

    return stamp(bar)


FAMILIES = {
    "F1_xs_momentum_12_1": (f1_xs_momentum, 21),
    "F2_short_term_reversal": (f2_short_term_reversal, 5),
    "F3_vol_compression_breakout": (_VolatilityCompressionBreakout, 10),
    "F4_gap_down_reversal": (f4_gap_down_reversal, 5),
    "F5_residual_reversal": (f5_residual_reversal, 5),
    "F6_live_selector_long": (_SelectorSignal, 5),
}


def make_signal(name: str):
    fn, horizon = FAMILIES[name]
    return (fn() if isinstance(fn, type) else fn), horizon
