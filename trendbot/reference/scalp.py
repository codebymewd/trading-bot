"""Step 2 reference: M15 setup zones + M5 trigger, gated by D1/H4. SIGNAL ONLY - no orders.

Flow per closed M5 bar:  D1+H4 gate -> M15 zone (FVG / sweep) -> M5 touch + structure
(BOS or displacement) trigger -> stop/spread/chase/premium-discount checks -> CANDIDATE.
Indicator confirmation (step 3), news/volatility and risk/basket checks come later.
Strictly causal: an M15 event is used only once its M15 bar has closed (time + 900 s <= M5 close).
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .engine import Engine, gate, htf_trend_at
from .indicators import IndParams, compute_series, confirm


@dataclass
class ScalpParams:
    gate_mode: str = "H4_AND_D1"
    fvg_setup: bool = True
    sweep_setup: bool = True
    require_bos_linked_fvg: bool = True
    trigger_window_bars: int = 12       # M5 bars after the zone is touched/armed
    sweep_setup_max_age_bars: int = 12  # M15 bars
    zone_max_age_bars: int = 24         # M15 bars (FVG zones)
    use_premium_discount: bool = True
    max_entry_range_pct: float = 50.0
    sl_buffer_atr: float = 0.1          # x M15 ATR
    min_sl_atr: float = 0.3
    max_sl_atr: float = 3.0
    max_chase_r: float = 0.5
    target_multiplier: float = 3.0
    max_spread_to_stop_ratio: float = 0.15
    spread: float = 0.0                 # price units; live EA reads the real spread
    enabled_indicators: tuple = ()      # step 3; empty + minimum 0 = structure only
    minimum_confirmations: int = 0
    ind: IndParams = field(default_factory=IndParams)
    setup_tf_seconds: int = 900
    trigger_tf_seconds: int = 300


@dataclass
class Signal:
    time: int        # open time of the M5 bar during which this became known
    type: str        # SETUP | SETUP_END | CANDIDATE | REJECT
    dir: int
    price: float = 0.0
    price2: float = 0.0
    price3: float = 0.0
    reason: str = ""
    detail: str = ""  # indicator confirmation summary, e.g. "RSI+ MACD0 BB+ STOCH+ (3/3)"


@dataclass
class Zone:
    kind: str        # FVG | SWEEP
    dir: int
    low: float
    high: float
    inval: float
    created_j: int
    expiry_j: int
    armed_t: int = -1
    born_t: int = -1   # M5 bar at which the zone became known; it can act only on LATER bars
    done: bool = False


class ScalpEngine:
    def __init__(self, d1: Engine, h4: Engine, m15: Engine, m5: Engine, params: Optional[ScalpParams] = None,
                 series=None):
        self.d1, self.h4, self.m15, self.m5 = d1, h4, m15, m5
        self.p = params or ScalpParams()
        self.series = series
        if self.series is None and self.p.enabled_indicators:
            self.series = compute_series(m5.o, m5.h, m5.l, m5.c, self.p.ind)
        self.signals: List[Signal] = []
        self.zones: List[Zone] = []
        self.m15_index: Dict[int, int] = {tm: i for i, tm in enumerate(m15.tm)}
        self.m5_events: Dict[int, list] = {}
        for e in m5.events:
            self.m5_events.setdefault(e.time, []).append(e)

    # ------------------------------------------------------------------
    def out(self, t, typ, d, price=0.0, price2=0.0, price3=0.0, reason="", detail=""):
        self.signals.append(Signal(self.m5.tm[t], typ, d, price, price2, price3, reason, detail))

    def run(self) -> List[Signal]:
        p, m5, m15 = self.p, self.m5, self.m15
        ptr = 0
        for t in range(m5.n):
            close_t = m5.tm[t] + p.trigger_tf_seconds
            g = gate(htf_trend_at(self.d1.tm, self.d1.trend_at, 86400, close_t),
                     htf_trend_at(self.h4.tm, self.h4.trend_at, 14400, close_t), p.gate_mode)
            while ptr < len(m15.events) and m15.events[ptr].time + p.setup_tf_seconds <= close_t:
                self.on_m15_event(m15.events[ptr], t, g)
                ptr += 1
            j = bisect_right(m15.tm, close_t - p.setup_tf_seconds) - 1
            if j < 0:
                continue
            self.update_zones(t, j, g)
        return self.signals

    # ------------------------------------------------------------------ zones
    def on_m15_event(self, e, t, g):
        p = self.p
        j = self.m15_index.get(e.time)
        if j is None:
            return
        if e.type == "FVG" and p.fvg_setup and (e.flag == 1 or not p.require_bos_linked_fvg):
            low, high = e.price, e.price2
            inval = low if e.dir == 1 else high
            self.zones.append(Zone("FVG", e.dir, low, high, inval, j, j + p.zone_max_age_bars, born_t=t))
            self.out(t, "SETUP", e.dir, low, high, g, "FVG")
        elif e.type == "SWEEP" and p.sweep_setup:
            d = -e.dir  # lows swept (dir -1) -> long setup; highs swept (dir +1) -> short setup
            if d == 1:
                low, high = e.price2, e.price
            else:
                low, high = e.price, e.price2
            self.zones.append(Zone("SWEEP", d, low, high, e.price2, j, j + p.sweep_setup_max_age_bars, armed_t=t, born_t=t))
            self.out(t, "SETUP", d, low, high, g, "SWEEP")
        elif e.type == "FVG_STATE" and e.flag in (3, 4, 5):
            names = {3: "filled", 4: "invalid", 5: "expired"}
            for z in self.zones:
                if not z.done and z.kind == "FVG" and z.dir == e.dir and z.low == e.price and z.high == e.price2:
                    z.done = True
                    self.out(t, "SETUP_END", z.dir, z.low, z.high, 0.0, names[e.flag])
        self.zones = [z for z in self.zones if not z.done]

    def update_zones(self, t, j, g):
        p, m5 = self.p, self.m5
        for z in self.zones:
            if z.done or t <= z.born_t:
                continue
            if j > z.expiry_j:
                self.end(z, t, "expired")
                continue
            if z.dir == 1 and m5.c[t] < z.inval or z.dir == -1 and m5.c[t] > z.inval:
                self.end(z, t, "invalidated")
                continue
            if z.dir != g:
                continue  # trend gate does not allow this direction right now
            if z.armed_t < 0:
                touched = m5.l[t] <= z.high if z.dir == 1 else m5.h[t] >= z.low
                if touched:
                    z.armed_t = t
            if z.armed_t < 0:
                continue
            if t - z.armed_t > p.trigger_window_bars:
                self.end(z, t, "trigger_timeout")
                continue
            if self.triggered(z, t):
                self.evaluate(z, t, j)
        self.zones = [z for z in self.zones if not z.done]

    def end(self, z, t, reason):
        z.done = True
        self.out(t, "SETUP_END", z.dir, z.low, z.high, 0.0, reason)

    def triggered(self, z, t) -> bool:
        m5 = self.m5
        bar_dir = 1 if m5.c[t] > m5.o[t] else (-1 if m5.c[t] < m5.o[t] else 0)
        if bar_dir != z.dir:
            return False
        if z.kind == "SWEEP":  # price must reclaim beyond the swept level
            if z.dir == 1 and m5.c[t] <= z.high:
                return False
            if z.dir == -1 and m5.c[t] >= z.low:
                return False
        return any(e.type in ("BOS", "DISP") and e.dir == z.dir for e in self.m5_events.get(m5.tm[t], []))

    def evaluate(self, z, t, j):
        self.decide(z, t, j)
        self.out(t, "SETUP_END", z.dir, z.low, z.high, 0.0, "consumed")

    def decide(self, z, t, j):
        p, m5, m15 = self.p, self.m5, self.m15
        atr = m15.atr[j]
        entry = m5.c[t]
        buf = p.sl_buffer_atr * atr
        sl = z.inval - buf if z.dir == 1 else z.inval + buf
        risk = abs(entry - sl)
        z.done = True
        if atr <= 0.0 or risk <= 0.0:
            self.out(t, "REJECT", z.dir, entry, sl, 0.0, "no_atr_or_zero_risk")
            return
        proximal = z.high if z.dir == 1 else z.low
        chase = ((entry - proximal) if z.dir == 1 else (proximal - entry)) / risk
        reason = ""
        if risk < p.min_sl_atr * atr:
            reason = "stop_too_tight"
        elif risk > p.max_sl_atr * atr:
            reason = "stop_too_wide"
        elif p.spread > p.max_spread_to_stop_ratio * risk:
            reason = "spread_too_wide"
        elif chase > p.max_chase_r:
            reason = "late_entry_chase"
        elif not self.discount_ok(z, j):
            reason = "premium_discount"
        if reason:
            self.out(t, "REJECT", z.dir, entry, sl, 0.0, reason)
            return
        detail = ""
        if p.enabled_indicators or p.minimum_confirmations:
            res = confirm(self.series, t, z.dir, p.enabled_indicators, p.minimum_confirmations, p.ind)
            detail = res.summary
            if not res.passed:  # structure was valid; the extra confirmation is insufficient
                self.out(t, "REJECT", z.dir, entry, sl, 0.0, "indicators_insufficient", detail)
                return
        target = entry + z.dir * p.target_multiplier * risk
        self.out(t, "CANDIDATE", z.dir, entry, sl, target, z.kind, detail)

    def discount_ok(self, z, j) -> bool:
        p, m15 = self.p, self.m15
        if not p.use_premium_discount:
            return True
        sh, sl = m15.sh_at[j], m15.sl_at[j]
        if sh is None or sl is None or sh <= sl:
            return True  # no valid dealing range: filter not applicable
        mid = (z.low + z.high) / 2.0
        pos = (mid - sl) / (sh - sl) * 100.0 if z.dir == 1 else (sh - mid) / (sh - sl) * 100.0
        return pos <= p.max_entry_range_pct
