"""Golden digest of the decision path on fixed synthetic data.

Recording-only changes (opportunity records, lifecycle status, instrument metadata) must NOT
change a single decision. The digest covers every trade (symbol, strategy, side, times, prices,
quantity) and the decision counters of a replay with the REAL selector and default config.
If a change is meant to alter decisions, it must update the digest in the same commit and say
why (and it changes the FWD decision-code fingerprint).
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from random import Random

from backtest.costs import CostModel
from market.history import PriceBar
from research.pipeline_backtest import PipelineBacktest, PipelineConfig

def _walk(seed, n=600):
    rng = Random(seed)
    out, price, t = [], 50.0 + seed, datetime(2021, 1, 4, 10)
    for _ in range(n):
        o = price
        c = max(1.0, o * (1 + rng.gauss(0.0003, 0.012)))
        out.append(PriceBar(t, o, max(o, c) * 1.004, min(o, c) * 0.996, c, 2e6 + rng.random() * 1e6))
        price = c
        t += timedelta(hours=1)
        if t.hour > 15:
            t = t.replace(hour=10) + timedelta(days=1)
    return out


def decision_digest() -> str:
    data = {f"S{i}": _walk(i) for i in range(8)}
    result = PipelineBacktest(data, PipelineConfig(cost_model=CostModel.zero())).run()
    parts = [f"{t.symbol}|{t.strategy}|{t.side}|{t.entry_time}|{t.exit_time}|{t.entry:.4f}|{t.exit:.4f}|{t.quantity}"
             for t in result.trade_log]
    parts.append(f"decisions={result.decisions} no_trade={result.no_trade_decisions} "
                 f"rejected={result.rejected_by_portfolio} submitted={result.submitted}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def test_decisions_are_unchanged_by_recording_only_changes():
    assert decision_digest() == EXPECTED_DIGEST


EXPECTED_DIGEST = "ea3bbeaa6328bf62502c0a80f48925aaa0dd73bcd10ecb428247b4745158136b"


def test_digest_is_deterministic_and_trades_exist():
    assert decision_digest() == decision_digest()
    data = {f"S{i}": _walk(i) for i in range(8)}
    assert PipelineBacktest(data, PipelineConfig(cost_model=CostModel.zero())).run().trades > 0
