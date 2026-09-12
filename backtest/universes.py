from __future__ import annotations

# Diagnostic cohorts only. These are NOT KNT's trading universe and are never used
# by discovery/execution. They exist so backtests are reproducible across different
# market personalities instead of being cherry-picked around one sector.
VALIDATION_UNIVERSES: dict[str, tuple[str, ...]] = {
    "broad_etf": ("SPY", "QQQ", "IWM", "DIA"),
    "mega_tech": ("AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL"),
    "high_vol_growth": ("TSLA", "AMD", "PLTR", "COIN", "MSTR"),
    "financials": ("JPM", "BAC", "GS", "MS", "WFC"),
    "energy": ("XOM", "CVX", "COP", "SLB", "OXY"),
    "defensive": ("KO", "PEP", "PG", "JNJ", "WMT"),
    "industrials": ("CAT", "DE", "GE", "HON", "UPS"),
    "semiconductors": ("NVDA", "AMD", "INTC", "AVGO", "QCOM"),
}


def universe_symbols(name: str) -> list[str]:
    key = name.strip().lower()
    if key == "all":
        seen: set[str] = set()
        result: list[str] = []
        for symbols in VALIDATION_UNIVERSES.values():
            for symbol in symbols:
                if symbol not in seen:
                    seen.add(symbol)
                    result.append(symbol)
        return result
    if key not in VALIDATION_UNIVERSES:
        raise KeyError(f"Unknown validation universe: {name}")
    return list(VALIDATION_UNIVERSES[key])
