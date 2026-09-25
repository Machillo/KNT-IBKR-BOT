"""Descriptive census of the CURRENT selector on TRAIN bars only (< 2023-09-01).

No returns are computed or looked at: it counts, per regime, which strategy the selector would
choose, how often NO_TRADE happens and why, and how often each strategy's signal is directional.
Uses the shared 140-bar decision context. Holdout and validation bars are never loaded past the
TRAIN boundary (bars are cut before evaluation).
"""
import sys
from collections import Counter, defaultdict

sys.path.insert(0, ".")
from engine.decision import DECISION_CONTEXT_BARS  # noqa: E402
from engine.strategy_selector import StrategySelector  # noqa: E402
from research.pipeline_backtest import _key  # noqa: E402
from research.protocol import VALIDATION_START  # noqa: E402
from run_pipeline_backtest import load  # noqa: E402
from config.config import reports_path  # noqa: E402

STEP = 5  # every 5th bar per symbol: descriptive only (run from the repo root: python tools/selector_census.py)
for profile in ("swing_5y", "long_10y"):
    data = load("all", profile, str(reports_path("history_cache")))
    selector = StrategySelector(55.0, None)
    chosen, reasons, regimes = Counter(), Counter(), Counter()
    by_regime = defaultdict(Counter)
    directional = Counter()
    evaluations = 0
    for symbol, bars in data.items():
        train = [b for b in bars if _key(b.time) < VALIDATION_START]
        for i in range(DECISION_CONTEXT_BARS, len(train), STEP):
            ctx = train[i - DECISION_CONTEXT_BARS:i]
            sel = selector.evaluate(ctx, symbol=symbol, asset_class="STK", timeframe="")
            evaluations += 1
            regimes[sel.regime.regime.value] += 1
            for e in sel.evaluations:
                if e.signal.side.value != "FLAT":
                    directional[e.strategy] += 1
            if sel.selected is None:
                reasons[sel.reason] += 1
                by_regime[sel.regime.regime.value]["NO_TRADE"] += 1
            else:
                chosen[sel.selected.strategy] += 1
                by_regime[sel.regime.regime.value][sel.selected.strategy] += 1
    print(f"=== {profile} TRAIN | evaluations={evaluations} symbols={len(data)}")
    print("regimes:", dict(regimes.most_common()))
    total_sel = sum(chosen.values())
    print(f"selected={total_sel} ({total_sel / evaluations:.1%}) no_trade_reasons={dict(reasons.most_common())}")
    print("selected by strategy:", {k: f"{v} ({v / max(1, total_sel):.0%})" for k, v in chosen.most_common()})
    print("directional signal rate by strategy:", {k: f"{v / evaluations:.1%}" for k, v in directional.most_common()})
    for regime, c in by_regime.items():
        n = sum(c.values())
        print(f"  {regime}: " + ", ".join(f"{k} {v / n:.0%}" for k, v in c.most_common(5)))
