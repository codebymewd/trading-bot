from types import SimpleNamespace

import pytest

from reference.engine import Engine, Event, Params
from reference.scalp import ScalpEngine, ScalpParams
from tests.test_engine import random_bars

T0 = 1_700_000_000 - (1_700_000_000 % 86400)  # a day boundary
M5S, M15S = 300, 900


def stub(tm, o=None, h=None, l=None, c=None, atr=10.0, trend=1, events=None, sh=None, sl=None):
    n = len(tm)
    ev = events or []
    return SimpleNamespace(tm=list(tm), n=n, o=o or [0.0] * n, h=h or [0.0] * n, l=l or [0.0] * n,
                           c=c or [0.0] * n, atr=[atr] * n, trend_at=[trend] * n, events=ev,
                           sh_at=sh or [None] * n, sl_at=sl or [None] * n)


def htf(trend_d1=1, trend_h4=1):
    return (stub([T0 - 3 * 86400, T0 - 86400], trend=trend_d1),
            stub([T0 - 8 * 3600, T0 - 4 * 3600], trend=trend_h4))


def m15_stub(events, n=40, sh=None, sl=None):
    tm = [T0 + k * M15S for k in range(n)]
    return stub(tm, atr=10.0, events=events, sh=sh, sl=sl)


def fvg_event(k, bot=1000.0, top=1004.0, d=1, linked=1):
    return Event(T0 + k * M15S, "FVG", d, bot, top, linked)


def m5_series(rows, n=150, base=1010.0):
    """rows: {index: (o,h,l,c)}; every other bar is flat at `base`."""
    tm = [T0 + i * M5S for i in range(n)]
    o, h, l, c = [base] * n, [base + 0.5] * n, [base - 0.5] * n, [base] * n
    for i, (oo, hh, ll, cc) in rows.items():
        o[i], h[i], l[i], c[i] = oo, hh, ll, cc
    return tm, o, h, l, c


def m5_stub(rows, events, n=150):
    tm, o, h, l, c = m5_series(rows, n)
    return stub(tm, o, h, l, c, atr=3.0, events=events)


def go(m15_events, m5_rows, m5_events, d1h4=None, **kw):
    d1, h4 = d1h4 or htf()
    m15 = kw.pop("m15", None) or m15_stub(m15_events)
    m5 = m5_stub(m5_rows, m5_events)
    eng = ScalpEngine(d1, h4, m15, m5, ScalpParams(**kw))
    return eng.run()


def types(sig, *names):
    return [s for s in sig if s.type in names]


def bos(i, d=1):
    return Event(T0 + i * M5S, "BOS", d, 1005.0, 0.0, 0)


# FVG on M15 bar 3 is known when that bar closes (= close of M5 bar 11) -> it can act from M5 bar 12 on
LONG_ROWS = {14: (1008.0, 1008.5, 1003.0, 1007.0),    # dips to 1003 (inside gap 1000-1004) but closes up
             15: (1006.0, 1007.5, 1005.5, 1007.0)}    # confirmation bar


def test_full_long_candidate():
    sig = go([fvg_event(3)], {14: (1004.5, 1008.5, 1003.0, 1006.5)}, [bos(14)])
    cand = types(sig, "CANDIDATE")
    assert len(cand) == 1
    c = cand[0]
    assert c.dir == 1 and c.reason == "FVG"
    assert c.price == 1006.5 and c.price2 == pytest.approx(999.0)   # inval 1000 - 0.1*ATR(10)
    risk = c.price - c.price2
    assert c.price3 == pytest.approx(c.price + 3 * risk)            # target = entry + multiplier x stop distance
    assert types(sig, "SETUP") and types(sig, "SETUP_END")[-1].reason == "consumed"


def test_gate_disagreement_blocks_everything():
    for d1h4 in (htf(1, -1), htf(-1, 1), htf(0, 1), htf(1, 0)):
        sig = go([fvg_event(3)], {14: (1004.5, 1008.5, 1003.0, 1006.5)}, [bos(14)], d1h4=d1h4)
        assert not types(sig, "CANDIDATE", "REJECT")


def test_h4_only_mode_ignores_d1():
    sig = go([fvg_event(3)], {14: (1004.5, 1008.5, 1003.0, 1006.5)}, [bos(14)], d1h4=htf(-1, 1), gate_mode="H4_ONLY")
    assert len(types(sig, "CANDIDATE")) == 1


def test_counter_gate_zone_is_not_traded():
    # bearish FVG zone while gate is bullish
    ev = [fvg_event(3, bot=1020.0, top=1024.0, d=-1)]
    rows = {14: (1019.0, 1021.0, 1018.5, 1019.5)}
    sig = go(ev, rows, [bos(14, -1)])
    assert not types(sig, "CANDIDATE", "REJECT")


def test_no_candidate_without_structure_confirmation():
    sig = go([fvg_event(3)], {14: (1004.5, 1008.5, 1003.0, 1006.5)}, [])
    assert not types(sig, "CANDIDATE")


def test_displacement_also_triggers():
    disp = Event(T0 + 14 * M5S, "DISP", 1, 1006.5, 1008.0, 1)
    sig = go([fvg_event(3)], {14: (1004.5, 1008.5, 1003.0, 1006.5)}, [disp])
    assert len(types(sig, "CANDIDATE")) == 1


def test_trigger_needs_touch_of_zone_first():
    rows = {14: (1010.0, 1012.0, 1009.5, 1011.5)}  # never reaches the gap (top 1004)
    sig = go([fvg_event(3)], rows, [bos(14)])
    assert not types(sig, "CANDIDATE")


def test_bearish_bar_cannot_trigger_long():
    sig = go([fvg_event(3)], {14: (1008.0, 1008.5, 1003.0, 1003.5)}, [bos(14)])
    assert not types(sig, "CANDIDATE")


def test_unlinked_fvg_ignored_by_default_but_allowed_when_configured():
    ev = [fvg_event(3, linked=0)]
    rows = {14: (1004.5, 1008.5, 1003.0, 1006.5)}
    assert not types(go(ev, rows, [bos(14)]), "SETUP")
    assert len(types(go(ev, rows, [bos(14)], require_bos_linked_fvg=False), "CANDIDATE")) == 1


def test_zone_cannot_act_on_the_bar_it_became_known():
    # M15 bar 3 closes with M5 bar 11; a touch+BOS on that very bar must not fire
    sig = go([fvg_event(3)], {11: (1004.5, 1008.5, 1003.0, 1006.5)}, [bos(11)])
    assert types(sig, "SETUP") and not types(sig, "CANDIDATE")


def test_event_not_used_before_its_m15_bar_closes():
    sig = go([fvg_event(3)], {10: (1004.5, 1008.5, 1003.0, 1006.5)}, [bos(10)])
    assert not types(sig, "CANDIDATE")  # M5 bar 10 closes before M15 bar 3 does
    assert [x.time for x in types(sig, "SETUP")] == [T0 + 11 * M5S]  # known only from M5 bar 11


def test_rejections():
    rows = {14: (1004.5, 1008.5, 1003.0, 1006.5)}
    r = lambda **kw: types(go([fvg_event(3)], rows, [bos(14)], **kw), "REJECT")
    assert r(min_sl_atr=0.9)[0].reason == "stop_too_tight"      # risk 7.5 < 9
    assert r(max_sl_atr=0.5)[0].reason == "stop_too_wide"       # risk 7.5 > 5
    assert r(spread=2.0)[0].reason == "spread_too_wide"         # 2 > 0.15 * 7.5
    far = {14: (1008.0, 1013.0, 1003.0, 1012.0)}
    assert types(go([fvg_event(3)], far, [bos(14)]), "REJECT")[0].reason == "late_entry_chase"


def test_premium_discount_filter():
    sh = [1030.0] * 40
    sl = [1000.0] * 40            # range 1000-1030, zone mid 1002 = 6.7% -> discount: allowed
    rows = {14: (1004.5, 1008.5, 1003.0, 1006.5)}
    d1, h4 = htf()
    m15 = m15_stub([fvg_event(3)], sh=sh, sl=sl)
    assert types(ScalpEngine(d1, h4, m15, m5_stub(rows, [bos(14)]), ScalpParams()).run(), "CANDIDATE")
    sh2 = [1004.0] * 40
    sl2 = [990.0] * 40            # zone mid 1002 is at 85.7% of 990-1004 -> premium: rejected
    m15b = m15_stub([fvg_event(3)], sh=sh2, sl=sl2)
    sig = ScalpEngine(d1, h4, m15b, m5_stub(rows, [bos(14)]), ScalpParams()).run()
    assert types(sig, "REJECT")[0].reason == "premium_discount"


def test_zone_is_consumed_once():
    rows = {14: (1004.5, 1008.5, 1003.0, 1006.5), 16: (1004.5, 1008.5, 1003.0, 1006.5)}
    sig = go([fvg_event(3)], rows, [bos(14), bos(16)])
    assert len(types(sig, "CANDIDATE")) == 1


def test_trigger_timeout_and_invalidation_and_expiry():
    touch = {14: (1008.0, 1008.5, 1003.0, 1003.8)}   # bearish bar: arms, no trigger
    late = {14: touch[14], 14 + 13: (1008.0, 1008.5, 1005.0, 1007.0)}
    sig = go([fvg_event(3)], late, [bos(27)])
    assert types(sig, "SETUP_END")[-1].reason == "trigger_timeout" and not types(sig, "CANDIDATE")
    inv = {14: (1008.0, 1008.5, 998.0, 999.0)}       # closes below the gap: invalidated
    assert types(go([fvg_event(3)], inv, [bos(14)]), "SETUP_END")[-1].reason == "invalidated"
    sig = go([fvg_event(3)], {}, [], zone_max_age_bars=2)
    assert types(sig, "SETUP_END")[-1].reason == "expired"


def test_fvg_state_event_ends_zone():
    st = Event(T0 + 5 * M15S, "FVG_STATE", 1, 1000.0, 1004.0, 3)
    sig = go([fvg_event(3), st], {}, [])
    assert types(sig, "SETUP_END")[-1].reason == "filled"


def test_sweep_setup_long_after_lows_swept():
    sweep = Event(T0 + 3 * M15S, "SWEEP", -1, 1005.0, 1002.0, 6)   # lows swept: level 1005, extreme 1002
    rows = {14: (1004.0, 1007.0, 1003.5, 1006.5)}                    # reclaims above the level with a bullish bar
    sig = go([sweep], rows, [bos(14)])
    cand = types(sig, "CANDIDATE")
    assert len(cand) == 1 and cand[0].dir == 1 and cand[0].reason == "SWEEP"
    # a close that stays under the swept level is not a reclaim
    weak = {14: (1004.0, 1004.9, 1003.5, 1004.6)}
    assert not types(go([sweep], weak, [bos(14)]), "CANDIDATE")


def test_sweep_of_highs_gives_short_when_gate_bearish():
    sweep = Event(T0 + 3 * M15S, "SWEEP", 1, 1020.0, 1024.0, 5)
    rows = {14: (1021.0, 1021.5, 1017.0, 1018.0)}
    sig = go([sweep], rows, [bos(14, -1)], d1h4=htf(-1, -1))
    c = types(sig, "CANDIDATE")
    assert len(c) == 1 and c[0].dir == -1 and c[0].price3 < c[0].price < c[0].price2


# ------------------------------------------------------- real engines, invariants
def aggregate(bars, k):
    t, o, h, l, c = bars
    out = ([], [], [], [], [])
    for i in range(0, len(t) - k + 1, k):
        out[0].append(t[i]); out[1].append(o[i]); out[2].append(max(h[i:i + k]))
        out[3].append(min(l[i:i + k])); out[4].append(c[i + k - 1])
    return out


def pipeline(m5_bars, bull=True):
    m5e = Engine(*m5_bars, Params())
    m5e.run()
    m15e = Engine(*aggregate(m5_bars, 3), Params())
    m15e.run()
    d1 = stub([T0 - 3 * 86400], trend=1 if bull else -1)
    h4 = stub([T0 - 8 * 3600], trend=1 if bull else -1)
    return ScalpEngine(d1, h4, m15e, m5e, ScalpParams(use_premium_discount=False)).run()


@pytest.mark.parametrize("seed", [1, 2, 3, 4])
def test_invariants_on_real_engines(seed):
    bars = random_bars(1500, seed)
    sig = pipeline(bars, bull=seed % 2 == 0)
    assert sig == sorted(sig, key=lambda s: s.time)
    for s in types(sig, "CANDIDATE"):
        risk = abs(s.price - s.price2)
        assert risk > 0
        if s.dir == 1:
            assert s.price2 < s.price < s.price3
        else:
            assert s.price3 < s.price < s.price2
        assert s.price3 == pytest.approx(s.price + s.dir * 3 * risk)
    assert len(types(sig, "SETUP")) >= 1


@pytest.mark.parametrize("seed", [5, 6])
def test_scalp_signals_never_repaint(seed):
    bars = random_bars(1500, seed)
    full = pipeline(bars, bull=True)
    assert len(full) > 5
    for k in (300, 603, 900, 1203):          # cut on M15 boundaries
        cut = tuple(x[:k] for x in bars)
        part = pipeline(cut, bull=True)
        limit = cut[0][-1]
        assert part == [s for s in full if s.time <= limit]


# ------------------------------------------------------- step 3: indicators come AFTER structure
def n_series(**kw):
    from tests.test_indicators import fake
    return fake(**kw)


def full_series(n, ok=True):
    """Series that make RSI/MACD/BB/STOCH all CONFIRM (ok) or all CONFLICT (not ok) for a long at bar 14."""
    from tests.test_indicators import fake
    sg = 1.0 if ok else -1.0
    if ok:   # K crosses above D at bar 13 and keeps rising
        k = [20.0] * 13 + [26.0] + [35.0] * (n - 14)
        d = [25.0] * 13 + [25.5] + [26.0] * (n - 14)
    else:    # K below D and falling
        k = [50.0 - i for i in range(n)]
        d = [60.0 - 0.5 * i for i in range(n)]
    return fake(close=[100.0 + 5 * sg] * n, rsi=[50.0 + 10 * sg] * n, macd=[sg] * n,
                macd_hist=[0.1 * i * sg for i in range(n)], stoch_k=k, stoch_d=d,
                bb_mid=[100.0] * n, bb_bw=[0.02 + 0.001 * i for i in range(n)], bb_bw_mean=[0.025] * n,
                cci=[10.0 * i * sg for i in range(n)])


def run_with_series(series, enabled, minimum, rows=None, **kw):
    d1, h4 = htf()
    m15 = m15_stub([fvg_event(3)])
    rows = rows or {14: (1004.5, 1008.5, 1003.0, 1006.5)}
    m5 = m5_stub(rows, [bos(14)])
    p = ScalpParams(enabled_indicators=tuple(enabled), minimum_confirmations=minimum, **kw)
    return ScalpEngine(d1, h4, m15, m5, p, series=series).run()


def test_indicators_confirm_a_valid_setup():
    sig = run_with_series(full_series(150, True), ["RSI", "MACD", "BB", "STOCH"], 3)
    c = types(sig, "CANDIDATE")
    assert len(c) == 1 and "(4/3)" in c[0].detail


def test_indicators_cannot_rescue_invalid_structure():
    # all indicators confirm, but the stop is too wide: structure checks win
    sig = run_with_series(full_series(150, True), ["RSI", "MACD", "BB", "STOCH"], 3, max_sl_atr=0.5)
    assert not types(sig, "CANDIDATE") and types(sig, "REJECT")[0].reason == "stop_too_wide"


def test_insufficient_indicators_reject_a_valid_setup():
    sig = run_with_series(full_series(150, False), ["RSI", "MACD", "BB", "STOCH"], 3)
    r = types(sig, "REJECT")
    assert not types(sig, "CANDIDATE") and r[0].reason == "indicators_insufficient" and "RSI-" in r[0].detail


def test_conflicting_indicators_do_not_trade_when_structure_is_invalid():
    sig = run_with_series(full_series(150, True), ["RSI"], 1, rows={14: (1004.5, 1008.5, 1003.0, 1003.8)})
    assert not types(sig, "CANDIDATE", "REJECT")  # bearish trigger bar: never reaches the indicator stage


def test_structure_only_mode_unchanged():
    sig = run_with_series(None, [], 0)
    assert len(types(sig, "CANDIDATE")) == 1 and types(sig, "CANDIDATE")[0].detail == ""
