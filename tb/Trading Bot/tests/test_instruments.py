import random

import pytest

from backend.app import instruments as ins
from backend.app.config_schema import (AssetClass, AssetClassParams, InstrumentState, SystemConfig, VolatilityConfig)
from backend.app.executor import volatility_state
from backend.app.instruments import ScoredCandidate, promote, score, select, validate_instrument
from backend.app.sizing import SymbolSpec

OK_SPEC = SymbolSpec("XAUUSDm", 0.01, 2, 0.01, 1.0, 1.0, 0.01, 100.0, 0.01)
FULL = {"D1": 400, "H4": 2000, "M15": 6000, "M5": 18000}
S = InstrumentState


def test_validation_requires_spec_history_and_supported_class():
    assert validate_instrument(OK_SPEC, FULL, "metals").ok
    assert "no_specification_from_terminal" in validate_instrument(None, FULL, "metals").reasons
    bad = SymbolSpec("X", 0.0, 2, 0.01, 1.0, 1.0, 0.01, 100.0, 0.01)
    assert not validate_instrument(bad, FULL, "metals").ok
    v = validate_instrument(OK_SPEC, {**FULL, "M5": 100}, "metals")
    assert not v.ok and any("insufficient_history_M5" in r for r in v.reasons)
    assert "asset_class_not_supported:other" in validate_instrument(OK_SPEC, FULL, "other").reasons
    ro = SymbolSpec("X", 0.01, 2, 0.01, 1.0, 1.0, 0.01, 100.0, 0.01, trade_allowed=False)
    assert "trading_not_allowed" in validate_instrument(ro, FULL, "forex").reasons


def test_lifecycle_needs_evidence_and_one_step_at_a_time():
    assert promote(S.DISCOVERED, S.SPEC_VALIDATED, {"spec_ok": True})[0]
    assert promote(S.DISCOVERED, S.SPEC_VALIDATED, {})[1] == "specification_not_validated"
    assert promote(S.DISCOVERED, S.MONITORED, {"history_ok": True})[1] == "promotion_must_go_one_step_at_a_time"
    assert promote(S.SPEC_VALIDATED, S.MONITORED, {})[1] == "insufficient_history"
    assert promote(S.MONITORED, S.DEMO_ELIGIBLE, {"oos_backtest_reviewed": True})[1].startswith("needs_out_of_sample")
    assert promote(S.MONITORED, S.DEMO_ELIGIBLE, {"oos_backtest_reviewed": True, "user_confirmed": True})[0]
    ev = {"demo_days": 14, "demo_rule_violations": 0, "user_confirmed": True}
    assert promote(S.DEMO_ELIGIBLE, S.LIVE_VALIDATED, ev)[0]
    assert not promote(S.DEMO_ELIGIBLE, S.LIVE_VALIDATED, {**ev, "demo_days": 13})[0]
    assert not promote(S.DEMO_ELIGIBLE, S.LIVE_VALIDATED, {**ev, "demo_rule_violations": 1})[0]
    assert not promote(S.DEMO_ELIGIBLE, S.LIVE_VALIDATED, {"demo_days": 30, "demo_rule_violations": 0})[0]   # no user sign-off


def test_blocking_is_always_allowed_and_blocked_cannot_skip_ahead():
    assert promote(S.LIVE_VALIDATED, S.TEMP_BLOCKED, {})[0] and promote(S.MONITORED, S.USER_DISABLED, {})[0]
    assert promote(S.USER_DISABLED, S.MONITORED, {"history_ok": True})[1] == "unblock_first"


def test_asset_class_volatility_thresholds_with_override():
    base = SystemConfig()
    assert volatility_state(1.6, 1.0, base, "EURUSD") == "elevated"          # global 1.5
    cfg = SystemConfig(asset_class_params={AssetClass.CRYPTO: AssetClassParams(
        volatility=VolatilityConfig(elevated_ratio=2.0, extreme_ratio=3.5))})
    assert volatility_state(1.6, 1.0, cfg, "BTCUSDm") == "normal"            # crypto tolerates more
    assert volatility_state(2.2, 1.0, cfg, "BTCUSDm") == "elevated"
    assert volatility_state(3.6, 1.0, cfg, "BTCUSDm") == "extreme"
    assert volatility_state(1.6, 1.0, cfg, "EURUSD") == "elevated"           # other classes keep the global values
    assert set(ins.ASSET_CLASS_VOLATILITY) == {"forex", "metals", "crypto"}


def cand(sym, d=1, conf=3, enabled=4, **kw):
    return ScoredCandidate(sym, d, kw.pop("kind", "FVG"), conf, enabled, **kw)


def test_score_rewards_completeness_and_penalises_cost():
    good = cand("XAUUSD", conf=4, bos_linked=True, sweep_present=True, spread_to_stop=0.02, cost_to_risk=0.02)
    weak = cand("XAUUSD", conf=2, bos_linked=False, spread_to_stop=0.14, cost_to_risk=0.14, volatility="elevated")
    assert score(good) > score(weak) and 0 <= score(weak) < score(good) <= 100
    assert score(cand("X", enabled=0)) > 0                                    # price-action-only setups still score


def test_selection_respects_slots_and_exposure_overlap():
    a = cand("XAUUSD", 1, conf=4, bos_linked=True)            # short USD
    b = cand("EURUSD", 1, conf=3)                             # also short USD -> overlaps with a
    c = cand("USDJPY", -1, conf=3)                            # short USD again (USDJPY sell = short USD, long JPY)
    d = cand("GBPJPY", 1, conf=3)                             # no USD exposure
    chosen = select([b, a, c, d], slots=2, policy="score")
    assert [x.symbol for x in chosen] == ["XAUUSD", "GBPJPY"]
    assert select([b, a, c, d], slots=1, policy="score")[0].symbol == "XAUUSD"
    assert select([], slots=3, policy="score") == []


def test_selection_policies_and_correlation():
    rnd = random.Random(1)
    px, series = 100.0, []
    for _ in range(150):
        px *= 1 + rnd.gauss(0, 0.002)
        series.append(px)
    x = cand("BTCUSD", 1, conf=4, closes=series)
    y = cand("ETHUSD", 1, conf=3, closes=[v * 2 for v in series])         # perfectly correlated, same direction
    chosen = select([y, x], slots=2, policy="score")
    assert [c.symbol for c in chosen] == ["BTCUSD"]
    cheap = cand("GBPJPY", 1, conf=2, cost_to_risk=0.01)
    dear = cand("EURAUD", 1, conf=4, cost_to_risk=0.12)
    assert select([dear, cheap], 1, "lowest_cost")[0].symbol == "GBPJPY"
    assert select([cheap, dear], 1, "score")[0].symbol == "EURAUD"
