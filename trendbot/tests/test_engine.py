import random

import pytest

from reference.engine import (BEAR, BULL, RANGE, Engine, Params, gate, htf_trend_at)

STEP = 300


def zigzag(waypoints, per_leg=10, start=1000.0, t0=1_700_000_000):
    """Bars along straight legs between waypoints (relative moves). Each bar ~2.5 range."""
    price = start
    o, h, l, c = [], [], [], []
    for move in waypoints:
        for k in range(per_leg):
            nxt = price + move / per_leg
            o.append(price); c.append(nxt)
            up = nxt >= price  # asymmetric wicks so reversals never tie on high/low
            h.append(max(price, nxt) + (0.5 if up else 0.1)); l.append(min(price, nxt) - (0.1 if up else 0.5))
            price = nxt
    t = [t0 + i * STEP for i in range(len(o))]
    return t, o, h, l, c


def run(bars, **kw):
    t, o, h, l, c = bars
    return Engine(t, o, h, l, c, Params(**kw))


def by(events, typ):
    return [e for e in events if e.type == typ]


def random_bars(n, seed):
    rnd = random.Random(seed)
    t0 = 1_700_000_000
    price, rows = 1000.0, []
    for i in range(n):
        o = price
        c = o + rnd.gauss(0, 2.0) + (0.15 if (i // 150) % 2 == 0 else -0.15)
        hi = max(o, c) + abs(rnd.gauss(0, 1.0))
        lo = min(o, c) - abs(rnd.gauss(0, 1.0))
        rows.append((t0 + i * STEP, o, hi, lo, c))
        price = c
    t, o, h, l, c = map(list, zip(*rows))
    return t, o, h, l, c


# ---------------------------------------------------------------- no look-ahead
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_events_never_repaint(seed):
    bars = random_bars(700, seed)
    full = run(bars).run()
    assert len(full) > 20
    for k in (150, 233, 400, 555, 699):
        t, o, h, l, c = (x[:k] for x in bars)
        part = Engine(t, o, h, l, c, Params()).run()
        cutoff = t[-1]
        expected = [e for e in full if e.time <= cutoff]
        assert part == expected, f"events differ when truncated at {k}"


def test_swing_known_only_after_right_bars():
    # one clear peak at index 40 inside a flat series
    t, o, h, l, c = zigzag([0.0] * 6)
    t, o, h, l, c = list(t), list(o), list(h), list(l), list(c)
    h[40] += 20.0
    eng = Engine(t, o, h, l, c, Params(min_swing_atr=0.0))
    ev = eng.run()
    sw = [e for e in ev if e.type == "SWING" and e.dir == 1 and abs(e.price - h[40]) < 1e-9]
    assert sw and sw[0].time == t[43]  # confirmed at i + swing_right, never earlier


# ---------------------------------------------------------------- structure
def uptrend_bars():
    # three up legs with pullbacks, then a deep drop that breaks the last low
    return zigzag([30, -12, 30, -12, 30, -12, 30, -12, 30])


def test_bullish_trend_and_bos():
    ev = run(uptrend_bars()).run()
    assert by(ev, "BOS") and any(e.dir == 1 for e in by(ev, "BOS"))
    trends = by(ev, "TREND")
    assert trends and trends[-1].dir == BULL


def test_mss_breaks_trend_and_requires_new_sequence():
    bars = zigzag([30, -12, 30, -12, 30, -12, 30, -12, 30, -60])
    eng = run(bars)
    ev = eng.run()
    mss = by(ev, "MSS")
    assert mss and mss[0].dir == -1
    assert eng.trend == RANGE or eng.trend == BEAR
    # trend must have gone to RANGE at the MSS bar
    trend_events = by(ev, "TREND")
    assert any(e.dir == RANGE and e.time == mss[0].time for e in trend_events)


def test_bearish_mirror():
    bars = zigzag([-30, 12, -30, 12, -30, 12, -30, 12, -30])
    ev = run(bars).run()
    assert by(ev, "TREND")[-1].dir == BEAR
    assert any(e.dir == -1 for e in by(ev, "BOS"))


def test_flat_market_is_range():
    t, o, h, l, c = zigzag([0.0] * 12)
    eng = Engine(t, o, h, l, c, Params())
    eng.run()
    assert eng.trend == RANGE


# ---------------------------------------------------------------- FVG
def fvg_series(gap_up=True, bos_free=True):
    # 20 flat warm-up bars, then c1, c2 (displacement), c3 with a gap
    n = 20
    t = [1_700_000_000 + i * STEP for i in range(n + 3)]
    o = [1000.0] * n; c = [1000.0] * n
    h = [1001.0] * n; l = [999.0] * n
    if gap_up:
        o += [1000.0, 1000.5, 1008.0]; c += [1000.4, 1008.0, 1009.0]
        h += [1001.0, 1008.2, 1010.0]; l += [999.0, 1000.4, 1007.0]
    else:
        o += [1000.0, 999.5, 992.0]; c += [999.6, 992.0, 991.0]
        h += [1001.0, 999.6, 992.8]; l += [999.0, 991.8, 990.0]
    return t, o, h, l, c


def test_bullish_fvg_detected():
    ev = run(fvg_series(True)).run()
    f = by(ev, "FVG")
    assert len(f) == 1 and f[0].dir == 1
    assert f[0].price == 1001.0 and f[0].price2 == 1007.0  # [high of c1, low of c3]


def test_bearish_fvg_detected():
    ev = run(fvg_series(False)).run()
    f = by(ev, "FVG")
    assert len(f) == 1 and f[0].dir == -1
    assert f[0].price == 992.8 and f[0].price2 == 999.0


def test_fvg_rejected_when_small_or_no_displacement():
    t, o, h, l, c = fvg_series(True)
    ev = Engine(t, o, h, l, c, Params(fvg_min_atr=50.0)).run()
    assert not by(ev, "FVG")
    t, o, h, l, c = fvg_series(True)
    o[21], c[21] = 1004.0, 1004.2  # c2 is no longer a displacement candle
    ev = Engine(t, o, h, l, c, Params()).run()
    assert not by(ev, "FVG")


def test_fvg_lifecycle_touch_half_fill_and_invalid():
    t, o, h, l, c = fvg_series(True)

    def extend(rows):
        for (oo, hh, ll, cc) in rows:
            t.append(t[-1] + STEP); o.append(oo); h.append(hh); l.append(ll); c.append(cc)

    extend([(1009, 1010, 1006.5, 1009.5),   # low <= top(1007): touched
            (1009, 1009.5, 1004.0, 1008)])  # low <= mid(1004): half
    ev = Engine(t, o, h, l, c, Params()).run()
    states = [e.flag for e in by(ev, "FVG_STATE")]
    assert states == [1, 2]
    extend([(1008, 1008.5, 1000.5, 1002.0)])  # low <= bot(1001) -> filled (mitigated)
    ev = Engine(t, o, h, l, c, Params()).run()
    assert [e.flag for e in by(ev, "FVG_STATE")] == [1, 2, 3]
    # close below the gap -> INVALID instead
    t2, o2, h2, l2, c2 = fvg_series(True)
    t, o, h, l, c = t2, o2, h2, l2, c2
    extend([(1009, 1009.5, 996.0, 997.0)])
    ev = Engine(t, o, h, l, c, Params()).run()
    assert [e.flag for e in by(ev, "FVG_STATE")] == [4]


def test_fvg_expires():
    t, o, h, l, c = fvg_series(True)
    for _ in range(60):
        t.append(t[-1] + STEP); o.append(1012.0); c.append(1012.0); h.append(1013.0); l.append(1011.0)
    ev = Engine(t, o, h, l, c, Params(fvg_max_age=10)).run()
    assert [e.flag for e in by(ev, "FVG_STATE")] == [5]


# ---------------------------------------------------------------- liquidity / sweeps
def sweep_series():
    # flat range with a clean swing high near 1010, later pierced and rejected
    t, o, h, l, c = zigzag([0.0] * 4, start=1000.0)
    t, o, h, l, c = list(t), list(o), list(h), list(l), list(c)
    h[10] = 1010.0           # swing high candidate
    return t, o, h, l, c


def append_bar(bars, oo, hh, ll, cc):
    t, o, h, l, c = bars
    t.append(t[-1] + STEP); o.append(oo); h.append(hh); l.append(ll); c.append(cc)


def test_sweep_requires_pierce_and_close_back():
    bars = sweep_series()
    append_bar(bars, 1000.0, 1010.8, 999.5, 1001.0)   # pierces 1010 by 0.8, closes back below
    ev = run(bars, min_swing_atr=0.0).run()
    sw = [e for e in by(ev, "SWEEP") if abs(e.price - 1010.0) < 1e-9 and e.flag == 5]  # 5 = swing high level
    assert len(sw) == 1 and sw[0].dir == 1 and sw[0].price2 == 1010.8


def test_accepted_break_is_not_a_sweep():
    bars = sweep_series()
    append_bar(bars, 1000.0, 1012.0, 999.5, 1011.5)   # closes above: acceptance
    append_bar(bars, 1011.5, 1013.0, 1010.5, 1012.5)  # still above on confirm bar
    ev = run(bars, min_swing_atr=0.0).run()
    assert not [e for e in by(ev, "SWEEP") if abs(e.price - 1010.0) < 1e-9]


def test_sweep_confirmed_on_second_bar():
    bars = sweep_series()
    append_bar(bars, 1000.0, 1011.5, 1000.0, 1011.0)  # pierces and closes above (pending)
    append_bar(bars, 1011.0, 1011.2, 1000.0, 1002.0)  # closes back below within window
    ev = run(bars, min_swing_atr=0.0).run()
    assert len([e for e in by(ev, "SWEEP") if abs(e.price - 1010.0) < 1e-9 and e.flag == 5]) == 1


def test_equal_highs_detected():
    t, o, h, l, c = zigzag([0.0] * 6, start=1000.0)
    h[10] = 1008.0
    l[20] = 995.0            # a swing low between the two highs
    h[30] = 1008.05          # equal within 0.1 ATR
    ev = Engine(t, o, h, l, c, Params(min_swing_atr=0.0)).run()
    eq = by(ev, "EQ")
    assert eq and eq[0].dir == 1 and eq[0].price == 1008.05


def test_previous_day_levels_created_and_swept():
    # two days of 5-minute bars; day 2 sweeps day 1 high
    day = 86400
    t0 = (1_700_000_000 // day + 1) * day  # server-day boundary
    t, o, h, l, c = [], [], [], [], []
    for i in range(2 * 288):
        t.append(t0 + i * STEP)
        base = 1000.0
        hi = base + 1.0
        if i == 100:
            hi = 1020.0  # day-1 high
        o.append(base); c.append(base); h.append(hi); l.append(base - 1.0)
    i = 288 + 50                      # on day 2: pierce and reject
    h[i], c[i] = 1021.0, 1000.0
    ev = Engine(t, o, h, l, c, Params()).run()
    swept = [e for e in by(ev, "SWEEP") if e.flag == 1]  # PDH
    assert len(swept) == 1 and swept[0].price == 1020.0 and swept[0].time == t[i]


# ---------------------------------------------------------------- HTF alignment & gate
def test_htf_lookup_uses_only_closed_bars():
    times = [0, 14400, 28800]
    trends = [1, -1, 1]
    assert htf_trend_at(times, trends, 14400, 14399) == RANGE   # first H4 bar still forming
    assert htf_trend_at(times, trends, 14400, 14400) == 1       # just closed
    assert htf_trend_at(times, trends, 14400, 28799) == 1       # second bar not closed yet
    assert htf_trend_at(times, trends, 14400, 28800) == -1


def test_gate_requires_agreement():
    assert gate(BULL, BULL) == BULL
    assert gate(BEAR, BEAR) == BEAR
    assert gate(BULL, BEAR) == RANGE
    assert gate(RANGE, BULL) == RANGE
    assert gate(BULL, RANGE) == RANGE
    assert gate(BULL, BEAR, "H4_ONLY") == BEAR
