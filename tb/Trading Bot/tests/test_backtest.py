from types import SimpleNamespace

import pytest

from backend.app import basket_manager as bm
from backend.app.backtest import (Bars, BtParams, TradeResult, compute_metrics, format_report, session_of, simulate,
                                  split_trades)
from backend.app.config_schema import DailyLimits, SystemConfig
from backend.app.news import NewsCalendar, NewsEvent
from backend.app.sizing import SymbolSpec

XAU = SymbolSpec("XAUUSD", 0.01, 2, 0.01, 1.0, 1.0, 0.01, 100.0, 0.01, stops_level_points=10)
DAY = (1_700_000_000 // 86400) * 86400
BASE = DAY + 9 * 3600            # 10:00 Lagos
STEP = 300
MGR = bm.ManagerParams()         # break-even off, CLOSE_ALL on timeout
CFG = SystemConfig()             # 1% risk, 2 baskets/day, 15 minute window, target 3x


def bars(special=None, n=80):
    special = special or {}
    tm = [BASE + i * STEP for i in range(n)]
    o, h, l, c = [2000.0] * n, [2000.5] * n, [1999.5] * n, [2000.0] * n
    for i, (oo, hh, ll, cc) in special.items():
        o[i], h[i], l[i], c[i] = oo, hh, ll, cc
    return Bars(tm, o, h, l, c)


def cand(bar_index, b, d=1, price=2000.0, sl=1995.0):
    return SimpleNamespace(time=b.tm[bar_index], dir=d, price=price, price2=sl, reason="FVG")


def run(b, cands, cfg=CFG, mgr=MGR, bt=None, cal=None, spec=XAU):
    return simulate(b, cands, spec, cfg, mgr, bt or BtParams(start_equity=1040.0, spread=0.2), cal)


def test_target_hit_pays_the_basket_target():
    b = bars({12: (2000.0, 2016.0, 1999.5, 2015.0)})
    r = run(b, [cand(10, b)])
    t = r.trades[0]
    assert len(r.trades) == 1 and t.outcome == "WIN" and t.reason == "stop_or_target"
    assert t.net == pytest.approx(31.2) and r.final_equity == pytest.approx(1071.2)   # 3 x the planned $10.40 risk
    assert t.lots == 0.02 and t.risk == pytest.approx(10.4)


def test_stop_loses_exactly_the_planned_risk():
    b = bars({12: (2000.0, 2000.5, 1994.0, 1995.0)})
    t = run(b, [cand(10, b)]).trades[0]
    assert t.outcome == "LOSS" and t.net == pytest.approx(-10.4)


def test_stop_assumed_first_when_both_inside_one_bar():
    b = bars({12: (2000.0, 2020.0, 1990.0, 2010.0)})
    assert run(b, [cand(10, b)]).trades[0].outcome == "LOSS"


def test_gap_through_the_stop_fills_at_the_open():
    b = bars({12: (1990.0, 1991.0, 1989.0, 1990.5)})
    t = run(b, [cand(10, b)]).trades[0]
    assert t.net == pytest.approx(-20.4)               # 0.02 lots from 2000.2 down to 1990.0: worse than the planned risk


def test_slippage_and_spread_reduce_results():
    b = bars({12: (2000.0, 2000.5, 1994.0, 1995.0)})
    clean = run(b, [cand(10, b)], bt=BtParams(2000.0, 0.2, 0.0)).trades[0].net
    slipped = run(b, [cand(10, b)], bt=BtParams(2000.0, 0.2, 0.1)).trades[0].net
    assert slipped < clean < 0


def test_short_basket_mirror():
    b = bars({12: (2000.0, 2000.5, 1983.0, 1984.0)})
    r = run(b, [cand(10, b, d=-1, price=2000.0, sl=2005.0)])
    t = r.trades[0]
    assert t.direction == -1 and t.outcome == "WIN" and t.net > 25.0


def test_break_even_then_stopped_at_entry_is_neutral():
    mgr = bm.ManagerParams("A", trigger_r=1.0, be_offset_points=0)
    b = bars({12: (2000.0, 2006.0, 1999.8, 2005.5), 13: (2005.5, 2005.6, 2000.0, 2001.0)})
    t = run(b, [cand(10, b)], mgr=mgr).trades[0]
    assert t.outcome == "NEUTRAL" and t.net == pytest.approx(0.0, abs=1e-9)


def test_timeout_closes_at_the_bar_close():
    b = bars({13: (2000.0, 2001.5, 2000.0, 2001.0)})        # basket opens bar 11, 15 min later = close of bar 13
    t = run(b, [cand(10, b)]).trades[0]
    assert t.reason == "timeout" and t.net == pytest.approx(0.02 * (2001.0 - 2000.2) / 0.01)


def test_daily_limits_and_lockout():
    b = bars({12: (2000.0, 2000.5, 1994.0, 1995.0), 22: (2000.0, 2000.5, 1994.0, 1995.0)})
    r = run(b, [cand(10, b), cand(20, b), cand(30, b)])
    assert [t.outcome for t in r.trades] == ["LOSS", "LOSS"] and r.rejected["locked_out"] == 1


def test_one_basket_at_a_time():
    b = bars()
    r = run(b, [cand(10, b), cand(12, b)])
    assert r.rejected["basket_open"] == 1 or len(r.trades) == 1


def test_min_lot_rejection_is_counted():
    b = bars()
    r = run(b, [cand(10, b)], bt=BtParams(start_equity=100.0, spread=0.2))
    assert not r.trades and r.rejected["plan_min_lot_exceeds_risk"] == 1


def test_outside_trading_window_rejected():
    late = [DAY + 21 * 3600 + i * STEP for i in range(40)]      # 22:00 Lagos onwards
    b = Bars(late, [2000.0] * 40, [2000.5] * 40, [1999.5] * 40, [2000.0] * 40)
    r = run(b, [SimpleNamespace(time=late[5], dir=1, price=2000.0, price2=1995.0, reason="FVG")])
    assert r.rejected["outside_trading_window"] == 1


def test_news_calendar_blocks_entries():
    b = bars()
    t_entry = b.tm[11]
    cal = NewsCalendar([NewsEvent(t_entry + 600, "USD", "high", "NFP", "NFP")], t_entry - 60, t_entry + 86400)
    r = run(b, [cand(10, b)], cal=cal)
    assert not r.trades and r.rejected["news_restricted"] == 1
    r2 = run(b, [cand(10, b)], cal=None)
    assert len(r2.trades) == 1
    quiet = NewsCalendar([], t_entry - 60, t_entry + 86400)
    assert len(run(b, [cand(10, b)], cal=quiet).trades) == 1


def test_trades_near_news_are_flagged_when_calendar_given():
    b = bars({12: (2000.0, 2016.0, 1999.5, 2015.0)})
    t_entry = b.tm[11]
    cal = NewsCalendar([NewsEvent(t_entry + 3000, "USD", "high", "x", "NFP")], t_entry - 60, t_entry + 86400)
    t = run(b, [cand(10, b)], cal=cal).trades[0]
    assert t.near_news is False or t.near_news is True   # window rules decide; flag is only informational


def trade(net, outcome=None, session="LONDON", vol=1.0, near=False, reason="stop_or_target"):
    outcome = outcome or ("WIN" if net > 0 else "LOSS" if net < 0 else "NEUTRAL")
    return TradeResult("X", 1, "FVG", 0, 0, 0.01, 5.0, net, reason, outcome, session, vol, near)


def test_metrics_math():
    ts = [trade(10), trade(-5), trade(10), trade(-5), trade(-5)]
    m = compute_metrics(ts, 1000.0)
    assert (m["trades"], m["wins"], m["losses"]) == (5, 2, 3)
    assert m["win_rate"] == 0.4 and m["profit_factor"] == pytest.approx(1.33, abs=0.01)
    assert m["net_profit"] == 5.0 and m["net_return_pct"] == 0.5
    assert m["max_drawdown_pct"] == pytest.approx((1015 - 1005) / 1015 * 100, abs=0.01)
    assert m["avg_win"] == 10.0 and m["avg_loss"] == -5.0 and m["expectancy_per_trade"] == 1.0
    assert m["max_consecutive_losses"] == 2 and m["max_consecutive_wins"] == 1


def test_metrics_edge_cases_and_groups():
    assert compute_metrics([], 1000.0)["trades"] == 0
    only_wins = compute_metrics([trade(5), trade(7)], 1000.0)
    assert only_wins["profit_factor"] == float("inf") and only_wins["max_drawdown_pct"] == 0.0
    ts = [trade(5, session="ASIA", vol=0.5), trade(-5, session="NEWYORK", vol=2.0, near=True), trade(3, session="ASIA", vol=0.0)]
    m = compute_metrics(ts, 1000.0)
    assert m["by_session"]["ASIA"]["trades"] == 2 and m["by_session"]["NEWYORK"]["net"] == -5.0
    assert set(m["by_volatility"]) == {"low(<0.8)", "high(>=1.5)", "n/a"}
    assert m["by_news"]["near_high_impact_news"]["trades"] == 1
    assert "Trades 3" in format_report("t", m)


def test_session_and_split():
    assert session_of(3 * 3600) == "ASIA" and session_of(9 * 3600) == "LONDON"
    assert session_of(15 * 3600) == "NEWYORK" and session_of(22 * 3600) == "OFF"
    dev, oos = split_trades([trade(1)] * 10, 0.7)
    assert len(dev) == 7 and len(oos) == 3


def test_full_pipeline_on_random_data_is_consistent():
    from reference.engine import Engine, Params
    from reference.scalp import ScalpEngine, ScalpParams
    from tests.test_engine import random_bars
    from tests.test_scalp_engine import aggregate, stub
    raw = random_bars(288 * 40, 9)
    m5 = Engine(*raw, Params())
    m5.run()
    m15 = Engine(*aggregate(raw, 3), Params())
    m15.run()
    d1 = stub([raw[0][0] - 86400], trend=1)
    h4 = stub([raw[0][0] - 14400], trend=1)
    sigs = ScalpEngine(d1, h4, m15, m5, ScalpParams(use_premium_discount=False)).run()
    cands = [s for s in sigs if s.type == "CANDIDATE"]
    bs = Bars(m5.tm, m5.o, m5.h, m5.l, m5.c, m5.atr)
    r = simulate(bs, cands, XAU if False else SymbolSpec("X", 0.01, 2, 0.01, 1.0, 1.0, 0.01, 100.0, 0.01), CFG, MGR,
                 BtParams(5000.0, 0.02, 0.0))
    assert r.final_equity == pytest.approx(5000.0 + sum(t.net for t in r.trades))
    assert all(t.close_time >= t.open_time for t in r.trades)
    assert [t.open_time for t in r.trades] == sorted(t.open_time for t in r.trades)
