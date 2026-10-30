"""Step 3 reference: indicator confirmation engine (causal, closed bars only).

Structure and price action are decided BEFORE this module is consulted. Indicators only add
confirmation, and they are not independent votes: each has one defined role.
  RSI    - momentum regime around the midline (not 30/70 reversals)
  MACD   - momentum direction: histogram turning/expanding with the line/zero-line side
  BB     - volatility: middle-band side plus expansion out of a squeeze (not band-touch reversals)
  STOCH  - pullback timing: %K turning back through %D in the trade direction
  CCI    - optional acceleration: zero-line side and rising/falling
A direction d is +1 (long) or -1 (short). Each indicator reports CONFIRM / NEUTRAL / CONFLICT;
only CONFIRM counts toward `minimum_confirmations`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

CONFIRM, NEUTRAL, CONFLICT = "CONFIRM", "NEUTRAL", "CONFLICT"
SQUEEZE_RATIO = 0.75       # bandwidth <= 75% of its recent mean = squeeze (no directional volatility yet)
RSI_NEUTRAL_BAND = 5.0     # within +/-5 of the midline = undecided, not a conflict
STOCH_CROSS_BARS = 3       # a K/D cross counts as "recent" for this many bars


@dataclass
class IndParams:
    rsi_period: int = 14
    rsi_midline: float = 50.0
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    stoch_k: int = 14
    stoch_d: int = 3
    stoch_slowing: int = 3
    bb_period: int = 20
    bb_deviation: float = 2.0
    cci_period: int = 14
    cci_threshold: float = 100.0


@dataclass
class ConfirmationResult:
    passed: bool
    count: int
    enabled: int
    minimum: int
    items: List[tuple]          # (name, status)

    @property
    def summary(self) -> str:
        mark = {CONFIRM: "+", NEUTRAL: "0", CONFLICT: "-"}
        return " ".join(f"{n}{mark[s]}" for n, s in self.items) + f" ({self.count}/{self.minimum})"


# ------------------------------------------------------------------ series
def ema(values: Sequence[float], period: int) -> List[float]:
    a = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(a * v + (1 - a) * out[-1])
    return out


def sma(values: Sequence[Optional[float]], period: int) -> List[Optional[float]]:
    out: List[Optional[float]] = []
    for i in range(len(values)):
        win = values[i - period + 1:i + 1] if i >= period - 1 else None
        out.append(sum(win) / period if win is not None and None not in win else None)
    return out


def rsi(closes: Sequence[float], period: int) -> List[Optional[float]]:
    n = len(closes)
    out: List[Optional[float]] = [None] * n
    if n <= period:
        return out
    gains = [max(closes[i] - closes[i - 1], 0.0) for i in range(1, n)]
    losses = [max(closes[i - 1] - closes[i], 0.0) for i in range(1, n)]
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    for i in range(period, n):
        if i > period:
            ag = (ag * (period - 1) + gains[i - 1]) / period
            al = (al * (period - 1) + losses[i - 1]) / period
        if al == 0.0:
            out[i] = 50.0 if ag == 0.0 else 100.0
        else:
            out[i] = 100.0 - 100.0 / (1.0 + ag / al)
    return out


def macd(closes: Sequence[float], fast: int, slow: int, signal: int):
    ef, es = ema(closes, fast), ema(closes, slow)
    line = [a - b for a, b in zip(ef, es)]
    sig = ema(line, signal)
    start = slow + signal - 2
    n = len(closes)
    m = [line[i] if i >= start else None for i in range(n)]
    s = [sig[i] if i >= start else None for i in range(n)]
    h = [line[i] - sig[i] if i >= start else None for i in range(n)]
    return m, s, h


def stochastic(h, l, c, kp, dp, slowing):
    n = len(c)
    raw: List[Optional[float]] = [None] * n
    for i in range(kp - 1, n):
        hh, ll = max(h[i - kp + 1:i + 1]), min(l[i - kp + 1:i + 1])
        raw[i] = 50.0 if hh == ll else 100.0 * (c[i] - ll) / (hh - ll)
    k = sma(raw, slowing)
    d = sma(k, dp)
    return k, d


def bollinger(closes, period, dev):
    n = len(closes)
    mid = sma(closes, period)
    bw: List[Optional[float]] = [None] * n
    for i in range(period - 1, n):
        win = closes[i - period + 1:i + 1]
        sd = (sum((x - mid[i]) ** 2 for x in win) / period) ** 0.5
        bw[i] = (2 * dev * sd) / mid[i] if mid[i] else 0.0
    return mid, bw


def cci(h, l, c, period):
    n = len(c)
    tp = [(h[i] + l[i] + c[i]) / 3.0 for i in range(n)]
    out: List[Optional[float]] = [None] * n
    for i in range(period - 1, n):
        win = tp[i - period + 1:i + 1]
        m = sum(win) / period
        md = sum(abs(x - m) for x in win) / period
        out[i] = 0.0 if md == 0.0 else (tp[i] - m) / (0.015 * md)
    return out


def compute_series(o, h, l, c, p: IndParams) -> Dict[str, list]:
    s: Dict[str, list] = {"close": list(c)}
    s["rsi"] = rsi(c, p.rsi_period)
    s["macd"], s["macd_signal"], s["macd_hist"] = macd(c, p.macd_fast, p.macd_slow, p.macd_signal)
    s["stoch_k"], s["stoch_d"] = stochastic(h, l, c, p.stoch_k, p.stoch_d, p.stoch_slowing)
    s["bb_mid"], s["bb_bw"] = bollinger(c, p.bb_period, p.bb_deviation)
    s["bb_bw_mean"] = sma(s["bb_bw"], p.bb_period)
    s["cci"] = cci(h, l, c, p.cci_period)
    return s


# ------------------------------------------------------------------ roles
def _g(s, key, t):
    return s[key][t] if 0 <= t < len(s[key]) else None


def eval_rsi(s, t, d, p):
    r = _g(s, "rsi", t)
    if r is None:
        return NEUTRAL
    a = (r - p.rsi_midline) * d
    return CONFIRM if a > 0 else (NEUTRAL if a >= -RSI_NEUTRAL_BAND else CONFLICT)


def eval_macd(s, t, d, p):
    m, h, hp = _g(s, "macd", t), _g(s, "macd_hist", t), _g(s, "macd_hist", t - 1)
    if m is None or h is None or hp is None:
        return NEUTRAL
    turning = (h - hp) * d
    if turning > 0 and (m * d > 0 or h * d > 0):
        return CONFIRM
    if turning < 0 and h * d < 0:
        return CONFLICT
    return NEUTRAL


def eval_bb(s, t, d, p):
    mid, bw, bwp, mean = _g(s, "bb_mid", t), _g(s, "bb_bw", t), _g(s, "bb_bw", t - 1), _g(s, "bb_bw_mean", t)
    if None in (mid, bw, bwp, mean):
        return NEUTRAL
    side = (s["close"][t] - mid) * d
    expanding = bw > bwp
    squeeze = bw <= SQUEEZE_RATIO * mean
    if side > 0 and expanding and not squeeze:
        return CONFIRM
    if side < 0 and expanding:
        return CONFLICT
    return NEUTRAL


def eval_stoch(s, t, d, p):
    k, dv, kp = _g(s, "stoch_k", t), _g(s, "stoch_d", t), _g(s, "stoch_k", t - 1)
    if None in (k, dv, kp):
        return NEUTRAL
    above = (k - dv) * d
    rising = (k - kp) * d
    crossed = False
    for u in range(t - STOCH_CROSS_BARS + 1, t + 1):
        k0, d0, k1, d1 = _g(s, "stoch_k", u - 1), _g(s, "stoch_d", u - 1), _g(s, "stoch_k", u), _g(s, "stoch_d", u)
        if None not in (k0, d0, k1, d1) and (k0 - d0) * d <= 0 < (k1 - d1) * d:
            crossed = True
    if above > 0 and rising > 0 and crossed:
        return CONFIRM
    if above < 0 and rising < 0:
        return CONFLICT
    return NEUTRAL


def eval_cci(s, t, d, p):
    x, xp = _g(s, "cci", t), _g(s, "cci", t - 1)
    if x is None or xp is None:
        return NEUTRAL
    if x * d > 0 and (x - xp) * d > 0:
        return CONFIRM
    if x * d < 0 and (x - xp) * d < 0:
        return CONFLICT
    return NEUTRAL


EVALUATORS = {"RSI": eval_rsi, "MACD": eval_macd, "BB": eval_bb, "STOCH": eval_stoch, "CCI": eval_cci}


def confirm(series, t: int, d: int, enabled: Sequence[str], minimum: int, p: IndParams) -> ConfirmationResult:
    if minimum < 0 or minimum > len(enabled):
        raise ValueError("minimum confirmations must be between 0 and the number of enabled indicators")
    items = []
    for name in enabled:
        if name not in EVALUATORS:
            raise ValueError(f"unknown indicator {name!r}")
        items.append((name, EVALUATORS[name](series, t, d, p)))
    count = sum(1 for _, st in items if st == CONFIRM)
    return ConfirmationResult(count >= minimum, count, len(enabled), minimum, items)
