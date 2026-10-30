import random

import pytest

from backend.app.sizing import (SymbolSpec, loss_per_lot, plan_basket, position_pnl, round_down_volume,
                                split_weights, target_price)

# Illustrative specs (NOT real broker values): the point is that sizing uses each symbol's own numbers.
XAU = SymbolSpec("XAUUSD", 0.01, 2, 0.01, 1.0, 1.0, 0.01, 100.0, 0.01, stops_level_points=10)
EUR = SymbolSpec("EURUSD", 0.00001, 5, 0.00001, 1.0, 1.0, 0.01, 200.0, 0.01, stops_level_points=0)
JPY = SymbolSpec("USDJPY", 0.001, 3, 0.001, 0.67, 0.67, 0.01, 100.0, 0.01)
BTC = SymbolSpec("BTCUSD", 0.01, 2, 0.01, 0.01, 0.01, 0.01, 50.0, 0.01, stops_level_points=500)
ALL = [XAU, EUR, JPY, BTC]
SCALE = {"XAUUSD": 2000.0, "EURUSD": 1.1, "USDJPY": 150.0, "BTCUSD": 60000.0}  # realistic price levels


def test_round_down_never_up():
    assert round_down_volume(0.01, 0.0299999) == 0.02
    assert round_down_volume(0.01, 0.03) == 0.03
    assert round_down_volume(0.1, 0.19) == 0.1
    assert round_down_volume(0.01, 0.009) == 0.0
    assert round_down_volume(0.01, -1) == 0.0


def test_spec_validation():
    assert XAU.problems() == []
    bad = SymbolSpec("X", 0.0, 2, 0.01, 1.0, 1.0, 0.01, 100.0, 0.01)
    assert "point must be > 0" in bad.problems()
    assert SymbolSpec("X", 0.01, 2, 0.01, 1.0, 1.0, 0.015, 100.0, 0.01).problems()  # min not multiple of step
    plan = plan_basket(bad, 1, 100.0, 99.0, 10.0)
    assert not plan.ok and plan.reason.startswith("invalid_symbol_spec")
    assert plan_basket(SymbolSpec("X", 0.01, 2, 0.01, 1, 1, 0.01, 100, 0.01, trade_allowed=False), 1, 100, 99, 10).reason == "trading_not_allowed"


def test_gold_example_lots_and_risk():
    # stop distance 5.00 = 500 ticks = $500 per lot -> $10 budget allows 0.02 lots
    p = plan_basket(XAU, 1, 2000.0, 1995.0, 10.0)
    assert p.ok and [x.lots for x in p.positions] == [0.02]
    assert p.planned_risk_money == pytest.approx(10.0)


def test_min_lot_over_budget_is_rejected_not_increased():
    p = plan_basket(XAU, 1, 2000.0, 1995.0, 3.0)   # needs 0.006 lots, minimum is 0.01 ($5 risk)
    assert not p.ok and p.reason == "min_lot_exceeds_risk"


def test_commission_is_part_of_the_risk():
    spec = SymbolSpec("XAUUSD", 0.01, 2, 0.01, 1.0, 1.0, 0.01, 100.0, 0.01, commission_per_lot=7.0)
    assert loss_per_lot(spec, 2000.0, 1995.0) == pytest.approx(507.0)
    p = plan_basket(spec, 1, 2000.0, 1995.0, 10.0)
    assert [x.lots for x in p.positions] == [0.01] and p.planned_risk_money == pytest.approx(5.07)


@pytest.mark.parametrize("spec", ALL, ids=lambda s: s.symbol)
@pytest.mark.parametrize("direction", [1, -1])
def test_risk_never_exceeds_budget_random(spec, direction):
    rnd = random.Random(42)
    checked = 0
    for _ in range(400):
        entry = SCALE[spec.symbol] * rnd.uniform(0.9, 1.1)
        min_dist = spec.stops_level_points * spec.point * 1.2 + spec.point
        dist = max(min_dist, rnd.uniform(0.0003, 0.01) * entry)
        sl = entry - direction * dist
        budget = rnd.uniform(5, 500)
        n = rnd.randint(1, 4)
        mode = rnd.choice(["equal", "front_loaded"])
        p = plan_basket(spec, direction, entry, sl, budget, n, mode)
        if not p.ok:
            assert p.reason in ("min_lot_exceeds_risk",)
            continue
        checked += 1
        assert p.planned_risk_money <= budget + 1e-6
        assert len(p.positions) <= n
        for x in p.positions:
            assert spec.volume_min <= x.lots <= spec.volume_max
            step_units = x.lots / spec.volume_step
            assert step_units == pytest.approx(round(step_units), abs=1e-6)
        # actual loss at the stop equals the planned risk
        loss = -sum(position_pnl(spec, direction, entry, sl, x.lots) for x in p.positions)
        assert loss == pytest.approx(p.planned_risk_money, rel=1e-6)
    assert checked > 20


def test_each_symbol_uses_its_own_numbers():
    risk = 10.0
    gold = plan_basket(XAU, 1, 2000.0, 1995.0, risk)
    eur = plan_basket(EUR, 1, 1.1000, 1.0950, risk)     # 50 pips = 500 ticks = $500/lot
    jpy = plan_basket(JPY, 1, 150.0, 149.5, risk)       # 0.5 = 500 ticks x 0.67 = $335/lot
    assert [x.lots for x in gold.positions] == [0.02]
    assert [x.lots for x in eur.positions] == [0.02]
    assert [x.lots for x in jpy.positions] == [0.02]    # floor(10/335 = 0.0298)


def test_split_weights():
    assert split_weights(1, "front_loaded") == [1.0]
    assert split_weights(3, "equal") == pytest.approx([1 / 3] * 3)
    w = split_weights(3, "front_loaded")
    assert sum(w) == pytest.approx(1.0) and w[0] == pytest.approx(0.394, abs=0.001) and w[1] == w[2]


def test_spec_example_three_positions_share_one_budget():
    # $100 account, 10% = $10; stop 1.00 on gold = $100 per lot
    p = plan_basket(XAU, 1, 2000.0, 1999.0, 10.0, max_positions=3, split_mode="front_loaded")
    assert p.ok and len(p.positions) == 3
    assert p.planned_risk_money <= 10.0           # NOT 3 x 10
    assert sum(x.risk_money for x in p.positions) == pytest.approx(p.planned_risk_money)


def test_fewer_positions_when_min_lot_makes_split_infeasible():
    p = plan_basket(XAU, 1, 2000.0, 1995.0, 10.0, max_positions=3)   # $500/lot: 3 x $3.33 would be < min lot
    assert p.ok and len(p.positions) == 2 and p.planned_risk_money == pytest.approx(10.0)
    assert p.requested_positions == 3


def test_stop_checks():
    assert plan_basket(XAU, 1, 2000.0, 2001.0, 10.0).reason == "sl_wrong_side"
    assert plan_basket(XAU, -1, 2000.0, 1999.0, 10.0).reason == "sl_wrong_side"
    assert plan_basket(XAU, 1, 2000.0, 1999.95, 10.0).reason == "stop_inside_broker_stop_level"  # 0.05 < 10 points
    assert plan_basket(XAU, 1, 2000.0, 1995.0, 0.0).reason == "no_risk_budget"
    assert plan_basket(XAU, 1, 2000.0, 1995.0, -5.0).reason == "no_risk_budget"


def test_margin_and_lot_cap():
    spec = SymbolSpec("XAUUSD", 0.01, 2, 0.01, 1.0, 1.0, 0.01, 100.0, 0.01, margin_per_lot=2000.0)
    ok = plan_basket(spec, 1, 2000.0, 1995.0, 10.0, free_margin=100.0)
    assert ok.ok and ok.margin_required == pytest.approx(40.0)
    assert plan_basket(spec, 1, 2000.0, 1995.0, 10.0, free_margin=30.0).reason == "insufficient_margin"
    capped = plan_basket(XAU, 1, 2000.0, 1999.0, 50.0, max_lot_per_position=0.2)   # would be 0.5 lots
    assert capped.ok and capped.positions[0].lots == 0.2 and capped.planned_risk_money == pytest.approx(20.0)
    assert plan_basket(XAU, 1, 2000.0, 1999.0, 50.0, max_lot_per_position=0.005).reason == "lot_cap_below_min_volume"


@pytest.mark.parametrize("spec", [XAU, EUR, JPY, BTC], ids=lambda s: s.symbol)
@pytest.mark.parametrize("direction", [1, -1])
def test_target_and_stop_money_match(spec, direction):
    spec = SymbolSpec(**{**spec.__dict__, "commission_per_lot": 4.0})
    entry = 1000.0 if spec.symbol != "EURUSD" else 1.1
    dist = max(spec.stops_level_points * spec.point * 3, spec.tick_size * 400)
    sl = entry - direction * dist
    p = plan_basket(spec, direction, entry, sl, 50.0, 2, "equal", target_multiplier=3.0)
    if not p.ok:
        pytest.skip(p.reason)
    at_sl = sum(position_pnl(spec, direction, entry, p.sl, x.lots) for x in p.positions)
    at_tp = sum(position_pnl(spec, direction, entry, p.tp, x.lots) for x in p.positions)
    assert at_sl == pytest.approx(-p.planned_risk_money, rel=1e-6)
    assert at_tp >= p.target_money - 1e-9                       # target reached (rounded in our favour)
    assert at_tp - p.target_money < p.total_lots * spec.tick_value_profit * (spec.point / spec.tick_size) * 1.01


def test_target_price_direction():
    assert target_price(XAU, 1, 2000.0, 0.02, 30.0) == pytest.approx(2015.0)
    assert target_price(XAU, -1, 2000.0, 0.02, 30.0) == pytest.approx(1985.0)
