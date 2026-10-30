import random

import pytest

from backend.app import basket_manager as bm
from backend.app.config_schema import PortfolioConfig
from backend.app.portfolio import exposure_vector, pct_returns, portfolio_check, rolling_correlation, shared_exposure
from backend.app.sizing import SymbolSpec, plan_basket


def spec(sym):
    return SymbolSpec(sym, 0.01, 2, 0.01, 1.0, 1.0, 0.01, 100.0, 0.01)


def basket(sym, d):
    plan = plan_basket(spec(sym), d, 100.0, 100.0 - d * 5, 10.0, 1)
    return bm.open_basket(sym, spec(sym), plan, [(1, 100.0)], 0, 900, 10.0)


def walk(seed, n=200, drift=0.0):
    r = random.Random(seed)
    x, out = 100.0, []
    for _ in range(n):
        x *= 1 + r.gauss(drift, 0.002)
        out.append(x)
    return out


def test_correlation_basics():
    a = walk(1)
    assert rolling_correlation(a, a, 100) == pytest.approx(1.0)
    assert rolling_correlation(a, [200.0 * 100.0 / v for v in a], 100) < -0.99          # inverse price series
    assert abs(rolling_correlation(a, walk(2), 100)) < 0.4
    assert rolling_correlation(a[:10], a[:10], 100) is None                              # too little data
    assert rolling_correlation([5.0] * 100, a[:100], 50) is None                         # zero variance


def test_pct_returns():
    assert pct_returns([100.0, 110.0, 99.0]) == pytest.approx([0.1, -0.1])


def test_exposure_vectors():
    assert exposure_vector("EURUSD", 1) == {"EUR": 1.0, "USD": -1.0}
    assert exposure_vector("EURUSD", -1) == {"EUR": -1.0, "USD": 1.0}
    assert exposure_vector("XAUUSDm", 1) == {"XAU": 1.0, "USD": -1.0}
    assert exposure_vector("US30", 1) == {}
    assert shared_exposure(exposure_vector("EURUSD", 1), exposure_vector("XAUUSD", 1)) == ["USD"]   # both short USD
    assert shared_exposure(exposure_vector("EURUSD", 1), exposure_vector("XAUUSD", -1)) == []        # hedge-like, no overlap


def test_no_open_baskets_is_always_fine():
    assert portfolio_check("EURUSD", 1, [], None, PortfolioConfig()) == ""


def test_shared_currency_exposure_blocks_same_direction_usd():
    cfg = PortfolioConfig()
    live = [basket("XAUUSD", 1)]
    closes = {"XAUUSD": walk(1), "EURUSD": walk(2)}
    assert portfolio_check("EURUSD", 1, live, closes, cfg) == "shared_currency_exposure"
    assert portfolio_check("EURUSD", -1, live, closes, cfg) == ""          # opposite USD exposure and uncorrelated
    off = PortfolioConfig(block_on_shared_currency_exposure=False)
    assert portfolio_check("EURUSD", 1, live, closes, off) == ""


def test_correlation_blocks_and_respects_direction():
    cfg = PortfolioConfig(block_on_shared_currency_exposure=False, correlation_block_threshold=0.8)
    a = walk(5)
    closes = {"XAUUSD": a, "BTCUSD": [v * 1.5 for v in a]}                  # perfectly correlated
    live = [basket("XAUUSD", 1)]
    assert portfolio_check("BTCUSD", 1, live, closes, cfg) == "correlated_exposure"
    assert portfolio_check("BTCUSD", -1, live, closes, cfg) == ""           # opposite side of a correlated pair hedges
    inv = {"XAUUSD": a, "BTCUSD": [10000.0 / v for v in a]}
    assert portfolio_check("BTCUSD", -1, live, inv, cfg) == "correlated_exposure"   # negative corr + opposite direction = same bet


def test_missing_data_fails_safe_when_exposure_exists():
    cfg = PortfolioConfig(block_on_shared_currency_exposure=False)
    live = [basket("XAUUSD", 1)]
    assert portfolio_check("BTCUSD", 1, live, None, cfg) == "correlation_data_unavailable"
    assert portfolio_check("BTCUSD", 1, live, {"XAUUSD": walk(1)}, cfg) == "correlation_data_unavailable"
    assert portfolio_check("BTCUSD", 1, live, {"XAUUSD": [1.0] * 5, "BTCUSD": [1.0] * 5}, cfg) == "correlation_data_unavailable"
