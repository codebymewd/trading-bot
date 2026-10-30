import math
import random

import pytest

from reference import indicators as ind
from reference.indicators import CONFIRM, CONFLICT, NEUTRAL, IndParams
from tests.test_engine import random_bars

P = IndParams()


def ohlc_from_closes(closes, spread=0.5):
    o = [closes[0]] + closes[:-1]
    h = [max(a, b) + spread for a, b in zip(o, closes)]
    l = [min(a, b) - spread for a, b in zip(o, closes)]
    return o, h, l, list(closes)


def series_of(closes, p=P):
    o, h, l, c = ohlc_from_closes(closes)
    return ind.compute_series(o, h, l, c, p)


RISING = [1000.0 + i * 1.0 for i in range(120)]
FALLING = [1120.0 - i * 1.0 for i in range(120)]
RND = [1000 + math.sin(i / 7.0) * 10 + random.Random(3).gauss(0, 1) * 0 for i in range(150)]


# ---------------------------------------------------------------- math properties
def test_ema_constant_and_seed():
    assert ind.ema([5.0] * 10, 4) == [5.0] * 10
    assert ind.ema([1.0, 2.0], 3)[0] == 1.0


def test_rsi_bounds_direction_and_symmetry():
    up, down = ind.rsi(RISING, 14), ind.rsi(FALLING, 14)
    assert up[13] is None and up[14] is not None
    assert up[-1] == 100.0 and down[-1] == 0.0
    rnd = [1000 + random.Random(9).gauss(0, 1) for _ in range(200)]
    r = [random.Random(i).gauss(0, 2) for i in range(300)]
    closes = [1000.0]
    for x in r:
        closes.append(closes[-1] + x)
    mirror = [2000.0 - x for x in closes]
    a, b = ind.rsi(closes, 14), ind.rsi(mirror, 14)
    for x, y in zip(a, b):
        if x is not None:
            assert 0.0 <= x <= 100.0
            assert x + y == pytest.approx(100.0, abs=1e-9)
    assert ind.rsi([100.0] * 30, 14)[-1] == 50.0


def test_rsi_matches_wilder_reference_values():
    # independent calculation of the first RSI value (simple average gain/loss over the first 14 changes)
    closes = [44.0, 44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61, 46.28]
    gains = [max(closes[i] - closes[i - 1], 0) for i in range(1, 15)]
    losses = [max(closes[i - 1] - closes[i], 0) for i in range(1, 15)]
    expected = 100 - 100 / (1 + (sum(gains) / 14) / (sum(losses) / 14))
    assert ind.rsi(closes, 14)[14] == pytest.approx(expected)
    assert 0.0 < expected < 100.0


def test_macd_signs_symmetry_and_relationship():
    m, s, h = ind.macd(RISING, 12, 26, 9)
    assert m[-1] > 0 and m[0] is None
    for a, b, c in zip(m, s, h):
        if a is not None:
            assert c == pytest.approx(a - b)
    m2, _, h2 = ind.macd([2000.0 - x for x in RISING], 12, 26, 9)
    assert m2[-1] == pytest.approx(-m[-1])


def test_stochastic_range_and_extremes():
    o, h, l, c = ohlc_from_closes(RISING)
    k, d = ind.stochastic(h, l, c, 14, 3, 3)
    assert all(0.0 <= x <= 100.0 for x in k if x is not None)
    assert k[-1] > 80
    o, h, l, c = ohlc_from_closes(FALLING)
    k2, _ = ind.stochastic(h, l, c, 14, 3, 3)
    assert k2[-1] < 20
    assert ind.stochastic([5.0] * 30, [5.0] * 30, [5.0] * 30, 14, 3, 3)[0][-1] == 50.0


def test_bollinger_constant_zero_width_and_expansion():
    mid, bw = ind.bollinger([100.0] * 40, 20, 2.0)
    assert bw[-1] == 0.0 and mid[-1] == 100.0
    calm = [100 + math.sin(i) * 0.5 for i in range(40)]
    wild = calm + [100 + math.sin(i) * 8 for i in range(20)]
    _, bw2 = ind.bollinger(wild, 20, 2.0)
    assert bw2[-1] > bw2[39]


def test_cci_symmetry_and_constant():
    o, h, l, c = ohlc_from_closes(RISING)
    x = ind.cci(h, l, c, 14)
    assert x[-1] is not None and x[12] is None
    o2, h2, l2, c2 = ohlc_from_closes([2000.0 - v for v in RISING])
    y = ind.cci(h2, l2, c2, 14)
    assert y[-1] == pytest.approx(-x[-1], rel=1e-6)
    assert ind.cci([5.0] * 30, [5.0] * 30, [5.0] * 30, 14)[-1] == 0.0


def test_series_are_causal():
    bars = random_bars(400, 21)
    o, h, l, c = bars[1], bars[2], bars[3], bars[4]
    full = ind.compute_series(o, h, l, c, P)
    for k in (100, 217, 333):
        part = ind.compute_series(o[:k], h[:k], l[:k], c[:k], P)
        for key in full:
            a, b = full[key][:k], part[key]
            for x, y in zip(a, b):
                assert (x is None and y is None) or x == pytest.approx(y, abs=1e-9), key


# ---------------------------------------------------------------- roles (rule logic)
def fake(**arrays):
    n = max(len(v) for v in arrays.values())
    base = {"close": [0.0] * n, "rsi": [None] * n, "macd": [None] * n, "macd_signal": [None] * n,
            "macd_hist": [None] * n, "stoch_k": [None] * n, "stoch_d": [None] * n, "bb_mid": [None] * n,
            "bb_bw": [None] * n, "bb_bw_mean": [None] * n, "cci": [None] * n}
    base.update(arrays)
    return base


def test_rsi_role_midline_not_extremes():
    s = fake(rsi=[20.0, 55.0, 48.0, 40.0, 85.0])
    assert ind.eval_rsi(s, 0, 1, P) == CONFLICT   # oversold is NOT a long signal
    assert ind.eval_rsi(s, 1, 1, P) == CONFIRM
    assert ind.eval_rsi(s, 2, 1, P) == NEUTRAL    # just below the midline
    assert ind.eval_rsi(s, 3, 1, P) == CONFLICT
    assert ind.eval_rsi(s, 4, -1, P) == CONFLICT  # overbought is NOT a short signal
    assert ind.eval_rsi(s, 3, -1, P) == CONFIRM
    assert ind.eval_rsi(fake(rsi=[None]), 0, 1, P) == NEUTRAL


def test_macd_role_histogram_turn_with_side():
    s = fake(macd=[None, 0.5, 0.5, -0.3, -0.3], macd_hist=[0.1, 0.3, 0.2, -0.1, -0.3])
    assert ind.eval_macd(s, 1, 1, P) == CONFIRM       # hist rising, above zero
    assert ind.eval_macd(s, 2, 1, P) == NEUTRAL       # still above zero but fading
    assert ind.eval_macd(s, 3, 1, P) == CONFLICT      # falling and negative
    assert ind.eval_macd(s, 4, -1, P) == CONFIRM      # falling histogram below zero helps shorts
    s2 = fake(macd=[None, -0.5, -0.5], macd_hist=[-0.3, -0.2, -0.1])
    assert ind.eval_macd(s2, 1, 1, P) == NEUTRAL      # contraction alone below both zero lines is not enough


def test_bollinger_role_expansion_and_squeeze():
    s = fake(close=[0, 105.0, 105.0, 95.0], bb_mid=[None, 100.0, 100.0, 100.0],
             bb_bw=[0.01, 0.03, 0.02, 0.05], bb_bw_mean=[0.04, 0.02, 0.04, 0.03])
    assert ind.eval_bb(s, 1, 1, P) == CONFIRM         # above middle, widening, not squeezed
    assert ind.eval_bb(s, 2, 1, P) == NEUTRAL         # bandwidth shrinking vs previous bar
    s_sq = fake(close=[0, 105.0], bb_mid=[None, 100.0], bb_bw=[0.01, 0.02], bb_bw_mean=[0.05, 0.05])
    assert ind.eval_bb(s_sq, 1, 1, P) == NEUTRAL      # still squeezed: no directional volatility
    assert ind.eval_bb(s, 3, 1, P) == CONFLICT        # below middle and expanding
    assert ind.eval_bb(s, 3, -1, P) == CONFIRM


def test_stochastic_role_turn_after_pullback():
    k = [20.0, 25.0, 35.0, 45.0, 44.0]
    d = [30.0, 28.0, 30.0, 33.0, 40.0]
    s = fake(stoch_k=k, stoch_d=d)
    assert ind.eval_stoch(s, 2, 1, P) == CONFIRM      # K crossed above D on this bar and is rising
    assert ind.eval_stoch(s, 3, 1, P) == CONFIRM      # cross was within the last 3 bars
    assert ind.eval_stoch(s, 4, 1, P) == NEUTRAL      # K above D but falling
    assert ind.eval_stoch(fake(stoch_k=[60.0, 55.0], stoch_d=[58.0, 59.0]), 1, 1, P) == CONFLICT
    # oversold alone is not a signal
    assert ind.eval_stoch(fake(stoch_k=[10.0, 8.0], stoch_d=[12.0, 11.0]), 1, 1, P) == CONFLICT


def test_cci_role_zero_line_and_acceleration():
    s = fake(cci=[-20.0, 30.0, 80.0, 70.0, -50.0, -90.0])
    assert ind.eval_cci(s, 1, 1, P) == CONFIRM
    assert ind.eval_cci(s, 3, 1, P) == NEUTRAL
    assert ind.eval_cci(s, 5, 1, P) == CONFLICT
    assert ind.eval_cci(s, 5, -1, P) == CONFIRM


# ---------------------------------------------------------------- confirmation engine
def all_confirm_long():
    n = 5
    return fake(close=[0, 0, 0, 0, 105.0], rsi=[None] * 4 + [60.0], macd=[None] * 4 + [1.0],
                macd_hist=[None, None, None, 0.1, 0.3], stoch_k=[None, 20.0, 25.0, 30.0, 40.0],
                stoch_d=[None, 25.0, 26.0, 29.0, 31.0], bb_mid=[None] * 4 + [100.0],
                bb_bw=[None, None, None, 0.02, 0.04], bb_bw_mean=[None] * 4 + [0.03], cci=[None, None, None, 10.0, 60.0])


def test_minimum_confirmations_threshold():
    s = all_confirm_long()
    en = ["RSI", "MACD", "BB", "STOCH"]
    r = ind.confirm(s, 4, 1, en, 3, P)
    assert r.passed and r.count == 4 and r.enabled == 4
    s["rsi"][4] = 40.0                       # one indicator turns against it
    r = ind.confirm(s, 4, 1, en, 4, P)
    assert not r.passed and r.count == 3
    assert ind.confirm(s, 4, 1, en, 3, P).passed
    assert "RSI-" in r.summary and r.summary.endswith("(3/4)")


def test_confirm_input_validation_and_price_action_only():
    s = all_confirm_long()
    assert ind.confirm(s, 4, 1, [], 0, P).passed              # price action only
    with pytest.raises(ValueError):
        ind.confirm(s, 4, 1, ["RSI"], 2, P)
    with pytest.raises(ValueError):
        ind.confirm(s, 4, 1, ["ADX"], 1, P)


def test_cci_is_optional_and_only_counts_when_enabled():
    s = all_confirm_long()
    base = ind.confirm(s, 4, 1, ["RSI", "MACD"], 2, P)
    with_cci = ind.confirm(s, 4, 1, ["RSI", "MACD", "CCI"], 3, P)
    assert base.count == 2 and with_cci.count == 3
