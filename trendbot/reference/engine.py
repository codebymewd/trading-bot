"""Stage A reference engine: swings, structure, displacement, FVG, liquidity, sweeps.

Bar-by-bar and strictly causal: step(t) reads bars <= t only, so signals can never
repaint or look ahead. The MQL5 engine (ea/Include/TrendBotEngine.mqh) mirrors this
file line by line; tools/compare_events.py checks the two agree on real broker data.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from typing import List, Optional

BULL, BEAR, RANGE = 1, -1, 0
KIND = {"PDH": 1, "PDL": 2, "PWH": 3, "PWL": 4, "SWH": 5, "SWL": 6, "EQH": 7, "EQL": 8,
        "ASH": 9, "ASL": 10, "LOH": 11, "LOL": 12, "NYH": 13, "NYL": 14}
KIND_NAME = {v: k for k, v in KIND.items()}
SINGLE_KINDS = {"PDH", "PDL", "PWH", "PWL", "ASH", "ASL", "LOH", "LOL", "NYH", "NYL"}
CAPPED_KINDS = {"SWH", "SWL", "EQH", "EQL"}
LEVEL_CAP = 30
SESSIONS = [("AS", 0, 8), ("LO", 8, 13), ("NY", 13, 21)]  # server-time hours [start, end)
MIT_LEVEL = {"touch": 1, "half": 2, "full": 3}
FVG_STATES = {0: "ACTIVE", 1: "TOUCHED", 2: "HALF", 3: "FILLED", 4: "INVALID", 5: "EXPIRED"}


@dataclass
class Params:
    atr_period: int = 14
    swing_left: int = 3
    swing_right: int = 3
    min_swing_atr: float = 0.5
    bos_buffer_atr: float = 0.05
    disp_body_atr: float = 1.2
    disp_body_ratio: float = 0.6
    disp_max_bars: int = 3
    eq_tol_atr: float = 0.1
    sweep_pierce_atr: float = 0.05
    sweep_confirm_bars: int = 2
    fvg_min_atr: float = 0.2
    fvg_min_points: int = 0
    fvg_max_age: int = 48
    fvg_mitigation: str = "full"
    point: float = 0.0


@dataclass
class Event:
    time: int
    type: str
    dir: int
    price: float
    price2: float
    flag: int
    trend: int = 0


@dataclass
class Swing:
    type: int
    idx: int
    price: float
    confirm: int
    broken: bool = False


@dataclass
class Level:
    kind: str
    side: int
    price: float
    created: int
    state: int = 0
    pierce: int = -1
    extreme: float = 0.0
    dead: bool = False


@dataclass
class Fvg:
    dir: int
    created: int
    bot: float
    top: float
    state: int = 0
    closed: bool = False


class Engine:
    def __init__(self, times, opens, highs, lows, closes, params: Optional[Params] = None):
        self.p = params or Params()
        self.tm, self.o, self.h, self.l, self.c = list(times), list(opens), list(highs), list(lows), list(closes)
        self.n = len(self.tm)
        self.tr: List[float] = []
        self.atr: List[float] = []
        self.trend_at: List[int] = []
        self.sh_at: List[Optional[float]] = []  # last confirmed swing high price known at bar t
        self.sl_at: List[Optional[float]] = []
        self.events: List[Event] = []
        self.swings: List[Swing] = []
        self.levels: List[Level] = []
        self.fvgs: List[Fvg] = []
        self.trend = RANGE
        self.lock = False
        self.lock_idx = -1
        self.last_bos_dir = 0
        self.last_bos_idx = -1
        self.bos_idx = {1: -99, -1: -99}
        self.last_disp_idx = -99
        self.last_disp_dir = 0
        self.cur_day = None
        self.day_h = self.day_l = 0.0
        self.cur_week = None
        self.week_h = self.week_l = 0.0
        self.sess_key = None
        self.sess_h = self.sess_l = 0.0

    # ------------------------------------------------------------------ helpers
    def emit(self, t, typ, d, price, price2=0.0, flag=0):
        self.events.append(Event(self.tm[t], typ, d, price, price2, flag))

    def dir_(self, i):
        return 1 if self.c[i] > self.o[i] else (-1 if self.c[i] < self.o[i] else 0)

    def body(self, i):
        return abs(self.c[i] - self.o[i])

    def rng(self, i):
        return self.h[i] - self.l[i]

    def last_swing(self, typ):
        for s in reversed(self.swings):
            if s.type == typ:
                return s
        return None

    def add_level(self, kind, side, price, created):
        if kind in SINGLE_KINDS:
            self.levels = [v for v in self.levels if v.kind != kind]
        elif kind in CAPPED_KINDS:
            same = [v for v in self.levels if v.kind == kind]
            if len(same) >= LEVEL_CAP:
                self.levels.remove(same[0])
        self.levels.append(Level(kind, side, price, created, extreme=price))

    # ------------------------------------------------------------------ main loop
    def run(self):
        for t in range(self.n):
            self.step(t)
        return self.events

    def step(self, t):
        p = self.p
        h, l, c = self.h, self.l, self.c
        ev_start = len(self.events)
        # 1. ATR (simple mean of true range)
        tr = h[t] - l[t] if t == 0 else max(h[t] - l[t], abs(h[t] - c[t - 1]), abs(l[t] - c[t - 1]))
        self.tr.append(tr)
        atr = 0.0
        if t >= p.atr_period - 1:
            s = 0.0
            for j in range(t - p.atr_period + 1, t + 1):
                s += self.tr[j]
            atr = s / p.atr_period
        self.atr.append(atr)
        # 2. calendar context -> PDH/PDL, PWH/PWL, session levels
        self.update_context(t)
        if atr <= 0.0:
            self.trend_at.append(RANGE)
            self.sh_at.append(None)
            self.sl_at.append(None)
            return
        # 3. swings
        R, L = p.swing_right, p.swing_left
        i = t - R
        if i >= L:
            if h[i] > max(h[i - L:i]) and h[i] > max(h[i + 1:i + R + 1]):
                self.add_swing(1, i, h[i], t)
            if l[i] < min(l[i - L:i]) and l[i] < min(l[i + 1:i + R + 1]):
                self.add_swing(-1, i, l[i], t)
        lasth, lastl = self.last_swing(1), self.last_swing(-1)
        self.sh_at.append(lasth.price if lasth is not None else None)
        self.sl_at.append(lastl.price if lastl is not None else None)
        buf = p.bos_buffer_atr * atr
        # 4. MSS (uses previous bar's trend), then BOS
        if self.trend == BULL and lastl is not None and c[t] < lastl.price - buf:
            self.emit(t, "MSS", -1, lastl.price)
            self.lock, self.lock_idx = True, t
        elif self.trend == BEAR and lasth is not None and c[t] > lasth.price + buf:
            self.emit(t, "MSS", 1, lasth.price)
            self.lock, self.lock_idx = True, t
        if lasth is not None and not lasth.broken and c[t] > lasth.price + buf:
            lasth.broken = True
            self.emit(t, "BOS", 1, lasth.price)
            self.last_bos_dir, self.last_bos_idx, self.bos_idx[1] = 1, t, t
        if lastl is not None and not lastl.broken and c[t] < lastl.price - buf:
            lastl.broken = True
            self.emit(t, "BOS", -1, lastl.price)
            self.last_bos_dir, self.last_bos_idx, self.bos_idx[-1] = -1, t, t
        # 5. trend
        highs = [s for s in self.swings if s.type == 1][-2:]
        lows = [s for s in self.swings if s.type == -1][-2:]
        raw = RANGE
        if len(highs) == 2 and len(lows) == 2:
            if highs[1].price > highs[0].price and lows[1].price > lows[0].price:
                raw = BULL
            elif highs[1].price < highs[0].price and lows[1].price < lows[0].price:
                raw = BEAR
        eff = raw
        if self.lock:
            if raw != RANGE and self.last_bos_dir == raw and self.last_bos_idx > self.lock_idx:
                self.lock = False
            else:
                eff = RANGE
        if eff != self.trend:
            self.trend = eff
            self.emit(t, "TREND", eff, c[t])
        self.trend_at.append(self.trend)
        # 6. displacement
        d = self.dir_(t)
        if d != 0:
            for k in range(1, p.disp_max_bars + 1):
                s0 = t - k + 1
                if s0 < 0:
                    break
                ok = True
                for j in range(s0, t + 1):
                    if self.dir_(j) != d or self.rng(j) <= 0.0 or self.body(j) / self.rng(j) < p.disp_body_ratio:
                        ok = False
                        break
                if not ok:
                    break
                if abs(c[t] - self.o[s0]) >= p.disp_body_atr * atr:
                    if not (self.last_disp_dir == d and self.last_disp_idx == t - 1):
                        self.emit(t, "DISP", d, c[t], self.o[s0], k)
                    self.last_disp_idx, self.last_disp_dir = t, d
                    break
        # 7. FVG creation then lifecycle
        self.fvg_create(t, atr)
        self.fvg_update(t)
        # 8. liquidity sweeps
        self.level_check(t, atr)
        for e in self.events[ev_start:]:
            e.trend = self.trend

    # ------------------------------------------------------------------ pieces
    def update_context(self, t):
        h, l = self.h[t], self.l[t]
        day = self.tm[t] // 86400
        week = (day + 3) // 7
        if self.cur_day is None:
            self.cur_day, self.day_h, self.day_l = day, h, l
            self.cur_week, self.week_h, self.week_l = week, h, l
        else:
            if day != self.cur_day:
                self.add_level("PDH", 1, self.day_h, t)
                self.add_level("PDL", -1, self.day_l, t)
                self.cur_day, self.day_h, self.day_l = day, h, l
            else:
                self.day_h, self.day_l = max(self.day_h, h), min(self.day_l, l)
            if week != self.cur_week:
                self.add_level("PWH", 1, self.week_h, t)
                self.add_level("PWL", -1, self.week_l, t)
                self.cur_week, self.week_h, self.week_l = week, h, l
            else:
                self.week_h, self.week_l = max(self.week_h, h), min(self.week_l, l)
        hour = (self.tm[t] % 86400) // 3600
        name = None
        for nm, a, b in SESSIONS:
            if a <= hour < b:
                name = nm
        key = (day, name) if name is not None else None
        if key != self.sess_key:
            if self.sess_key is not None:
                self.add_level(self.sess_key[1] + "H", 1, self.sess_h, t)
                self.add_level(self.sess_key[1] + "L", -1, self.sess_l, t)
            self.sess_key = key
            self.sess_h, self.sess_l = h, l
        elif key is not None:
            self.sess_h, self.sess_l = max(self.sess_h, h), min(self.sess_l, l)

    def add_swing(self, typ, idx, price, t):
        p = self.p
        opp = self.last_swing(-typ)
        if opp is not None and abs(price - opp.price) < p.min_swing_atr * self.atr[t]:
            return
        last = self.swings[-1] if self.swings else None
        if last is not None and last.type == typ:
            if (typ == 1 and price > last.price) or (typ == -1 and price < last.price):
                kind = "SWH" if typ == 1 else "SWL"
                for v in self.levels:
                    if v.kind == kind and v.price == last.price and v.created == last.confirm:
                        self.levels.remove(v)
                        break
                self.swings[-1] = Swing(typ, idx, price, t)
            else:
                return
        else:
            self.swings.append(Swing(typ, idx, price, t))
        self.emit(t, "SWING", typ, price)
        kind = "SWH" if typ == 1 else "SWL"
        self.add_level(kind, typ, price, t)
        best, bd = None, 0.0
        for v in self.levels:
            if v.kind == kind and not (v.created == t and v.price == price):
                dd = abs(v.price - price)
                if dd <= p.eq_tol_atr * self.atr[t] and (best is None or dd < bd):
                    best, bd = v, dd
        if best is not None:
            lvl = max(price, best.price) if typ == 1 else min(price, best.price)
            ek = "EQH" if typ == 1 else "EQL"
            self.add_level(ek, typ, lvl, t)
            self.emit(t, "EQ", typ, lvl)

    def fvg_create(self, t, atr):
        if t < 2:
            return
        p = self.p
        h, l = self.h, self.l
        dirn, bot, top = 0, 0.0, 0.0
        if l[t] > h[t - 2]:
            dirn, bot, top = 1, h[t - 2], l[t]
        elif h[t] < l[t - 2]:
            dirn, bot, top = -1, h[t], l[t - 2]
        if dirn == 0:
            return
        r2 = self.rng(t - 1)
        c2disp = r2 > 0.0 and self.body(t - 1) >= p.disp_body_atr * atr and self.body(t - 1) / r2 >= p.disp_body_ratio
        size = top - bot
        if size >= max(p.fvg_min_atr * atr, p.fvg_min_points * p.point) and c2disp and self.dir_(t - 1) == dirn:
            linked = 1 if self.bos_idx[dirn] >= t - 2 else 0
            self.fvgs.append(Fvg(dirn, t, bot, top))
            self.emit(t, "FVG", dirn, bot, top, linked)

    def fvg_update(self, t):
        p = self.p
        h, l, c = self.h, self.l, self.c
        for f in self.fvgs:
            if f.closed or f.created == t:
                continue
            mid = (f.bot + f.top) / 2.0
            if f.dir == 1:
                comp = 4 if c[t] < f.bot else 3 if l[t] <= f.bot else 2 if l[t] <= mid else 1 if l[t] <= f.top else 0
            else:
                comp = 4 if c[t] > f.top else 3 if h[t] >= f.top else 2 if h[t] >= mid else 1 if h[t] >= f.bot else 0
            new = 4 if comp == 4 else max(f.state, comp)
            if new != f.state:
                f.state = new
                self.emit(t, "FVG_STATE", f.dir, f.bot, f.top, new)
            if f.state == 4 or f.state >= MIT_LEVEL[p.fvg_mitigation]:
                f.closed = True
            elif t - f.created > p.fvg_max_age:
                f.state = 5
                self.emit(t, "FVG_STATE", f.dir, f.bot, f.top, 5)
                f.closed = True
        self.fvgs = [f for f in self.fvgs if not f.closed]

    def level_check(self, t, atr):
        p = self.p
        h, l, c = self.h, self.l, self.c
        buf = p.sweep_pierce_atr * atr
        for v in self.levels:
            if v.side == 1:
                if v.state == 0:
                    if h[t] > v.price + buf:
                        v.extreme = h[t]
                        if c[t] < v.price:
                            self.sweep(t, v)
                        elif p.sweep_confirm_bars <= 1:
                            v.dead = True
                        else:
                            v.state, v.pierce = 1, t
                elif v.state == 1:
                    v.extreme = max(v.extreme, h[t])
                    if c[t] < v.price:
                        self.sweep(t, v)
                    elif t - v.pierce >= p.sweep_confirm_bars - 1:
                        v.dead = True
            else:
                if v.state == 0:
                    if l[t] < v.price - buf:
                        v.extreme = l[t]
                        if c[t] > v.price:
                            self.sweep(t, v)
                        elif p.sweep_confirm_bars <= 1:
                            v.dead = True
                        else:
                            v.state, v.pierce = 1, t
                elif v.state == 1:
                    v.extreme = min(v.extreme, l[t])
                    if c[t] > v.price:
                        self.sweep(t, v)
                    elif t - v.pierce >= p.sweep_confirm_bars - 1:
                        v.dead = True
        self.levels = [v for v in self.levels if not v.dead]

    def sweep(self, t, v):
        self.emit(t, "SWEEP", v.side, v.price, v.extreme, KIND[v.kind])
        v.dead = True


def htf_trend_at(times, trend_at, tf_seconds, t_close):
    """Trend of the latest higher-TF bar that is fully closed at t_close (no look-ahead)."""
    j = bisect_right(times, t_close - tf_seconds) - 1
    return trend_at[j] if j >= 0 else RANGE


def gate(d1_trend, h4_trend, mode="H4_AND_D1"):
    if mode == "H4_ONLY":
        return h4_trend
    return d1_trend if d1_trend == h4_trend else RANGE
