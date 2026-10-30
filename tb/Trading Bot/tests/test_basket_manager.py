import copy

import pytest

from backend.app import basket_manager as bm
from backend.app.basket_manager import ManagerParams
from backend.app.config_schema import BreakEvenConfig, RiskConfig, ScalpConfig, SystemConfig
from backend.app.sizing import SymbolSpec, plan_basket
from backend.app.trading_day import DailyCounters

XAU = SymbolSpec("XAUUSD", 0.01, 2, 0.01, 1.0, 1.0, 0.01, 100.0, 0.01, stops_level_points=10)
T0 = 1_700_000_000
WINDOW = 900


def mk(direction=1, positions=1, risk=10.0, spec=XAU, entry=2000.0, dist=5.0, max_loss=None):
    sl = entry - direction * dist
    plan = plan_basket(spec, direction, entry, sl, risk, positions)
    assert plan.ok, plan.reason
    fills = [(i + 1, entry) for i in range(len(plan.positions))]
    return bm.open_basket("B1", spec, plan, fills, T0, WINDOW, max_loss or plan.planned_risk_money), plan


def ev(b, bid, now=T0 + 10, p=None, ask=None, **kw):
    return bm.evaluate(b, now, bid, bid + 0.2 if ask is None else ask, p or ManagerParams(), **kw)


def kinds(actions):
    return [(a.kind, a.reason) for a in actions]


# ---------------------------------------------------------------- P/L
def test_pnl_long_and_short_use_exit_side():
    b, _ = mk(1)
    assert bm.basket_pnl(b, 2005.0, 2005.2) == pytest.approx(10.0)    # long exits at bid
    s, _ = mk(-1)
    assert bm.basket_pnl(s, 1994.8, 1995.0) == pytest.approx(10.0)    # short exits at ask
    assert bm.basket_pnl(b, 1995.0, 1995.2) == pytest.approx(-10.0)


def test_commission_reduces_pnl_and_target_is_net():
    spec = SymbolSpec("XAUUSD", 0.01, 2, 0.01, 1.0, 1.0, 0.01, 100.0, 0.01, stops_level_points=10, commission_per_lot=7.0)
    b, plan = mk(1, spec=spec, risk=20.0)
    assert bm.basket_pnl(b, 2000.0, 2000.2) == pytest.approx(-7.0 * b.total_lots)
    assert [a.reason for a in ev(b, plan.tp, ask=plan.tp + 0.2)] == ["target"]      # reaches net target at tp
    assert ev(b, plan.tp - 0.5, ask=plan.tp - 0.3) == []


def test_evaluate_is_pure():
    b, _ = mk(1, 2)
    before = copy.deepcopy(b)
    ev(b, 2005.0, p=ManagerParams("A"))
    ev(b, 2015.0)
    assert b == before


# ---------------------------------------------------------------- target / max loss
def test_target_and_max_loss():
    b, plan = mk(1, 2)
    assert plan.tp == pytest.approx(2015.0)
    assert kinds(ev(b, 2015.0)) == [("CLOSE_ALL", "target")]
    assert ev(b, 2014.9) == []
    assert kinds(ev(b, 1995.0)) == [("CLOSE_ALL", "max_loss")]
    b2, _ = mk(1, 1, max_loss=8.0)       # a tighter basket max-loss than the stop
    assert kinds(ev(b2, 1996.0)) == [("CLOSE_ALL", "max_loss")]


def test_short_basket_mirror():
    b, plan = mk(-1, 2)
    assert plan.tp == pytest.approx(1985.0)
    assert kinds(ev(b, 1984.8, ask=1985.0)) == [("CLOSE_ALL", "target")]
    assert kinds(ev(b, 2004.8, ask=2005.0)) == [("CLOSE_ALL", "max_loss")]


# ---------------------------------------------------------------- timeout
def test_timeout_close_all():
    b, _ = mk()
    assert ev(b, 2002.0, now=T0 + WINDOW - 1) == []
    assert kinds(ev(b, 2002.0, now=T0 + WINDOW, p=ManagerParams(timeout_action="CLOSE_ALL"))) == [("CLOSE_ALL", "timeout")]


def test_timeout_close_if_loss():
    b, _ = mk()
    p = ManagerParams(timeout_action="CLOSE_IF_LOSS")
    assert kinds(ev(b, 1998.0, now=T0 + WINDOW, p=p)) == [("CLOSE_ALL", "timeout_loss")]
    acts = ev(b, 2002.0, now=T0 + WINDOW, p=p)
    assert [a.kind for a in acts] == ["MARK_TIMEOUT"]
    bm.apply_internal(b, acts)
    assert ev(b, 1998.0, now=T0 + 2 * WINDOW, p=p) == []     # timeout already handled: keeps running


def test_timeout_keep_running():
    b, _ = mk()
    acts = ev(b, 1998.0, now=T0 + WINDOW, p=ManagerParams(timeout_action="KEEP_RUNNING"))
    assert [a.kind for a in acts] == ["MARK_TIMEOUT"]
    bm.apply_internal(b, acts)
    assert b.timeout_handled and ev(b, 1998.0, now=T0 + 5000, p=ManagerParams(timeout_action="KEEP_RUNNING")) == []


def test_timeout_move_to_breakeven():
    p = ManagerParams(timeout_action="MOVE_TO_BREAKEVEN", be_offset_points=0)
    b, _ = mk(1, 2)
    acts = ev(b, 2003.0, now=T0 + WINDOW, p=p)
    assert kinds(acts) == [("MODIFY_SL", "timeout_breakeven")] and acts[0].detail["sl"] == {1: 2000.0, 2: 2000.0}
    # not applied until the broker confirms
    assert all(pos.sl == 1995.0 for pos in b.positions) and not b.be_done
    assert kinds(ev(b, 2003.0, now=T0 + WINDOW + 5, p=p)) == [("MODIFY_SL", "timeout_breakeven")]   # re-issued
    bm.confirm_sl_moved(b, acts[0].detail["sl"], timeout=True)
    assert all(pos.sl == 2000.0 for pos in b.positions) and ev(b, 2003.0, now=T0 + WINDOW + 9, p=p) == []
    # in loss, or too close to price for the broker stop level: cannot move, keep original stops
    for bid in (1998.0, 2000.05):
        b2, _ = mk(1, 2)
        acts = ev(b2, bid, now=T0 + WINDOW, p=p)
        assert [a.kind for a in acts] == ["MARK_TIMEOUT"], bid


# ---------------------------------------------------------------- break-even
def test_break_even_off_by_default():
    b, _ = mk()
    assert ev(b, 2014.0) == []


def test_break_even_method_a_trigger_offset_and_confirmation():
    p = ManagerParams("A", trigger_r=1.0, be_offset_points=20)
    b, _ = mk(1, 2)
    assert ev(b, 2004.9, p=p) == []
    acts = ev(b, 2005.0, p=p)
    assert kinds(acts) == [("MODIFY_SL", "break_even")] and acts[0].detail["sl"] == {1: 2000.2, 2: 2000.2}
    assert not b.be_done and b.positions[0].sl == 1995.0          # nothing claimed before confirmation
    bm.confirm_sl_moved(b, acts[0].detail["sl"])
    assert b.be_done and b.positions[0].sl == 2000.2 and ev(b, 2008.0, p=p) == []


def test_break_even_trigger_half_r_and_short():
    p = ManagerParams("A", trigger_r=0.5)
    b, _ = mk(1)
    assert ev(b, 2002.4, p=p) == [] and kinds(ev(b, 2002.5, p=p)) == [("MODIFY_SL", "break_even")]
    s, _ = mk(-1)
    acts = ev(s, 1997.4, ask=1997.5, p=p)
    assert kinds(acts) == [("MODIFY_SL", "break_even")] and acts[0].detail["sl"] == {1: 2000.0}


def test_break_even_never_loosens_a_stop():
    p = ManagerParams("A", trigger_r=1.0, be_offset_points=0)
    b, _ = mk()
    b.positions[0].sl = 2001.0                      # already tighter than break-even
    assert ev(b, 2006.0, p=p) == []


def test_break_even_respects_broker_stop_level():
    p = ManagerParams("A", trigger_r=0.001, be_offset_points=0)   # triggers immediately
    b, _ = mk()
    assert ev(b, 2000.05, p=p) == []                # only 0.05 above BE, broker needs >= 0.10
    assert kinds(ev(b, 2000.2, p=p)) == [("MODIFY_SL", "break_even")]


def test_method_b_partial_close_then_move():
    p = ManagerParams("B", trigger_r=1.0, partial_close_percent=50.0)
    b, _ = mk(1, 1, risk=20.0)
    assert b.positions[0].lots == 0.04
    acts = ev(b, 2005.0, p=p)
    assert kinds(acts) == [("PARTIAL_CLOSE", "break_even_partial"), ("MODIFY_SL", "break_even")]
    assert acts[0].detail["lots"] == {1: 0.02}
    bm.confirm_partial(b, 1, 0.02, realized_pnl=10.0)
    bm.confirm_sl_moved(b, acts[1].detail["sl"])
    assert b.positions[0].lots == pytest.approx(0.02) and b.realized == 10.0 and b.partial_done
    assert bm.basket_pnl(b, 2005.0, 2005.2) == pytest.approx(10.0 + 10.0)   # booked + floating
    # a position at minimum size cannot be split: only the stop moves
    b2, _ = mk(1, 1, risk=5.0)
    assert [a.kind for a in ev(b2, 2005.0, p=p)] == ["MODIFY_SL"]


def test_method_c_structural_stop():
    p = ManagerParams("C", trigger_r=1.0)
    b, _ = mk()
    assert ev(b, 2005.0, p=p) == []                               # no structural point supplied yet
    acts = ev(b, 2005.0, p=p, structural_sl=1999.0)
    assert kinds(acts) == [("MODIFY_SL", "break_even")] and acts[0].detail["sl"] == {1: 1999.0}
    assert ev(b, 2005.0, p=p, structural_sl=1990.0) == []         # would loosen the stop
    assert ev(b, 2002.0, p=p, structural_sl=1999.0) == []         # 1R not reached yet


# ---------------------------------------------------------------- lifecycle / risk
def test_open_risk_money_follows_stops():
    b, _ = mk(1, 2)
    assert bm.open_risk_money(b) == pytest.approx(10.0)
    bm.confirm_sl_moved(b, {1: 1999.0, 2: 1999.0})
    assert bm.open_risk_money(b) == pytest.approx(2.0)            # 0.02 lots x 100 ticks
    bm.confirm_sl_moved(b, {1: 2000.5, 2: 2000.5})
    assert bm.open_risk_money(b) == 0.0                           # stop in profit


def test_closed_basket_is_inert_and_nothing_can_be_added():
    b, _ = mk()
    bm.confirm_closed(b, 29.9, "target")
    assert b.status == "CLOSED" and b.final_pnl == 29.9 and b.positions == []
    assert ev(b, 2020.0) == [] and bm.can_add_position(b) is False
    open_b, _ = mk()
    assert bm.can_add_position(open_b) is False                    # no averaging/martingale, even before target


def test_open_basket_requires_accepted_plan():
    bad = plan_basket(XAU, 1, 2000.0, 1995.0, 3.0)
    with pytest.raises(ValueError):
        bm.open_basket("x", XAU, bad, [], T0, WINDOW, 3.0)
    ok = plan_basket(XAU, 1, 2000.0, 1995.0, 10.0, 2)
    with pytest.raises(ValueError):
        bm.open_basket("x", XAU, ok, [(1, 2000.0)], T0, WINDOW, 10.0)


# ---------------------------------------------------------------- exposure & daily counters
def cfg_multi(**kw):
    return SystemConfig(risk=RiskConfig(risk_percent=10, max_open_risk_percent=10, max_simultaneous_positions=3),
                        scalp=ScalpConfig(max_positions_per_basket=3), **kw)


def test_exposure_checks():
    cfg = cfg_multi()
    plan = plan_basket(XAU, 1, 2000.0, 1999.0, 10.0, 3)
    c = DailyCounters()
    assert bm.exposure_check([], plan, "XAUUSD", 100.0, cfg, c) == ""
    live, _ = mk(1, 1)
    other = SymbolSpec("EURUSD", 0.00001, 5, 0.00001, 1.0, 1.0, 0.01, 200.0, 0.01)
    plan_eur = plan_basket(other, 1, 1.1, 1.095, 10.0, 1)
    assert bm.exposure_check([live], plan, "XAUUSD", 100.0, cfg, c) == "symbol_already_open"
    assert bm.exposure_check([live], plan_eur, "EURUSD", 100.0, cfg, c) == "aggregate_risk_cap"   # 10 + 10 > 10
    bm.confirm_sl_moved(live, {1: 2000.5})                                                           # risk-free now
    assert bm.exposure_check([live], plan_eur, "EURUSD", 100.0, cfg, c) == ""
    plan3 = plan_basket(XAU, 1, 2000.0, 1999.0, 10.0, 3)
    live_eur, _ = mk(1, 1, spec=other, entry=1.1, dist=0.005, risk=5.0)
    assert bm.exposure_check([live_eur], plan3, "XAUUSD", 1000.0, cfg, c) == "position_cap"   # 1 open + 3 new > 3
    big = SystemConfig()   # default: 1 position max
    assert bm.exposure_check([], plan3, "XAUUSD", 100000.0, big, c) == "position_cap"


def test_exposure_blocked_by_daily_state():
    cfg = cfg_multi()
    plan = plan_basket(XAU, 1, 2000.0, 1999.0, 10.0, 1)
    c = DailyCounters(locked_out=True)
    assert bm.exposure_check([], plan, "XAUUSD", 100.0, cfg, c) == "locked_out"
    assert bm.exposure_check([], plan, "XAUUSD", 100.0, cfg, DailyCounters(clock_anomaly=True)) == "clock_anomaly"
    assert bm.exposure_check([], plan, "XAUUSD", 100.0, cfg, DailyCounters(trades_opened=2)) == "daily_basket_limit"


def test_daily_counters_basket_accounting():
    c = DailyCounters()
    assert c.can_open_basket(2)
    c.on_basket_opened()
    assert c.on_basket_closed(-4.0, 0.5, 2) == "LOSS" and not c.locked_out and c.consecutive_losses == 1
    c.on_basket_opened()
    assert not c.can_open_basket(2)                                  # 2 baskets used
    assert c.on_basket_closed(-1.0, 0.5, 2) == "LOSS" and c.locked_out and c.losses == 2


def test_neutral_and_win_outcomes():
    c = DailyCounters()
    c.on_basket_closed(-3.0, 0.5, 5)
    assert c.on_basket_closed(0.3, 0.5, 5) == "NEUTRAL" and c.consecutive_losses == 1 and c.losses == 1
    assert c.on_basket_closed(12.0, 0.5, 5) == "WIN" and c.consecutive_losses == 0
    assert c.realized_net == pytest.approx(9.3)


def test_consecutive_and_daily_loss_limits_lock():
    c = DailyCounters()
    c.on_basket_closed(-1.0, 0.0, 10, max_consecutive=2)
    assert not c.locked_out
    c.on_basket_closed(-1.0, 0.0, 10, max_consecutive=2)
    assert c.locked_out
    d = DailyCounters()
    d.on_basket_closed(-6.0, 0.0, 10, daily_loss_limit_money=5.0)
    assert d.locked_out


# ---------------------------------------------------------------- config mapping and a full flow
def test_params_from_config():
    cfg = SystemConfig(break_even=BreakEvenConfig(method="B", trigger_r=1.5, be_offset_points=10, partial_close_percent=40),
                       scalp=ScalpConfig(timeout_action="CLOSE_IF_LOSS"))
    p = bm.params_from_config(cfg)
    assert (p.break_even_method, p.trigger_r, p.be_offset_points, p.partial_close_percent, p.timeout_action) == \
           ("B", 1.5, 10, 40.0, "CLOSE_IF_LOSS")


def test_full_flow_break_even_then_stopped_out_is_neutral():
    cfg = cfg_multi()
    p = ManagerParams("A", trigger_r=1.0, be_offset_points=0, timeout_action="KEEP_RUNNING")
    c = DailyCounters()
    plan = plan_basket(XAU, 1, 2000.0, 1995.0, 10.0, 2)
    assert bm.exposure_check([], plan, "XAUUSD", 100.0, cfg, c) == ""
    c.on_basket_opened()
    b = bm.open_basket("F1", XAU, plan, [(1, 2000.0), (2, 2000.0)], T0, WINDOW, plan.planned_risk_money)
    acts = bm.evaluate(b, T0 + 60, 2005.1, 2005.3, p)
    bm.confirm_sl_moved(b, acts[0].detail["sl"])
    assert bm.open_risk_money(b) == 0.0
    # price falls back and the break-even stop is hit: broker closes both positions at 2000.0
    net = sum(bm.position_pnl(XAU, 1, pos.entry, pos.sl, pos.lots) for pos in b.positions)
    bm.confirm_closed(b, net, "stop")
    assert c.on_basket_closed(net, 0.5, 2) == "NEUTRAL" and not c.locked_out and c.consecutive_losses == 0
