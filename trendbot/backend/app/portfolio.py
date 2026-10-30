"""Portfolio-aware filters: shared currency exposure and rolling correlation with open baskets.
Correlation is a risk filter, not a guarantee: it can break down at any time."""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence

from .config_schema import PortfolioConfig
from .news import currencies_for_symbol

MIN_RETURNS = 20


def pct_returns(closes: Sequence[float]) -> List[float]:
    return [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes)) if closes[i - 1] != 0]


def rolling_correlation(a: Sequence[float], b: Sequence[float], lookback: int) -> Optional[float]:
    ra, rb = pct_returns(a)[-lookback:], pct_returns(b)[-lookback:]
    n = min(len(ra), len(rb))
    if n < MIN_RETURNS:
        return None
    ra, rb = ra[-n:], rb[-n:]
    ma, mb = sum(ra) / n, sum(rb) / n
    va = sum((x - ma) ** 2 for x in ra)
    vb = sum((y - mb) ** 2 for y in rb)
    if va <= 0 or vb <= 0:
        return None
    return sum((x - ma) * (y - mb) for x, y in zip(ra, rb)) / math.sqrt(va * vb)


def exposure_vector(symbol: str, direction: int) -> Dict[str, float]:
    cur = currencies_for_symbol(symbol)
    if len(cur) == 2:                      # forex pair: long = +base, -quote
        return {cur[0]: float(direction), cur[1]: -float(direction)}
    if len(cur) == 1:                      # metal/crypto priced in USD
        head = "".join(ch for ch in symbol.upper() if ch.isalpha())[:3]
        return {head: float(direction), "USD": -float(direction)}
    return {}


def shared_exposure(a: Dict[str, float], b: Dict[str, float]) -> List[str]:
    return sorted(c for c in a if c in b and a[c] * b[c] > 0)


def portfolio_check(new_symbol: str, new_dir: int, open_baskets, closes: Optional[Dict[str, Sequence[float]]],
                    cfg: PortfolioConfig) -> str:
    """'' when the new basket adds no excessive shared or correlated exposure, else a reason code."""
    live = [b for b in open_baskets if b.status == "OPEN"]
    if not live:
        return ""
    new_vec = exposure_vector(new_symbol, new_dir)
    for b in live:
        if cfg.block_on_shared_currency_exposure and shared_exposure(new_vec, exposure_vector(b.spec.symbol, b.direction)):
            return "shared_currency_exposure"
        if closes is None or new_symbol not in closes or b.spec.symbol not in closes:
            return "correlation_data_unavailable"
        corr = rolling_correlation(closes[new_symbol], closes[b.spec.symbol], cfg.correlation_lookback_bars)
        if corr is None:
            return "correlation_data_unavailable"
        if corr * new_dir * b.direction >= cfg.correlation_block_threshold:
            return "correlated_exposure"
    return ""
