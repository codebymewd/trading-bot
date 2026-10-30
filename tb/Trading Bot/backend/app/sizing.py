"""Broker-aware position sizing for one scalp basket.

Never exceeds the risk budget: volumes are rounded DOWN to the broker step, commission is part of
the loss per lot, and a trade whose minimum lot would risk more than the budget is rejected
(risk is never silently increased). All money is in account currency; MT5 tick values already are.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import List, Optional

FRONT_LOAD_FACTOR = 1.3  # first position of a front-loaded basket gets 1.3x a later one's share


@dataclass(frozen=True)
class SymbolSpec:
    symbol: str
    point: float
    digits: int
    tick_size: float
    tick_value_profit: float     # account currency per tick per 1 lot (gain)
    tick_value_loss: float       # account currency per tick per 1 lot (loss)
    volume_min: float
    volume_max: float
    volume_step: float
    stops_level_points: int = 0
    freeze_level_points: int = 0
    commission_per_lot: float = 0.0   # round turn, account currency
    margin_per_lot: float = 0.0       # estimate; the EA uses OrderCalcMargin
    trade_allowed: bool = True

    def problems(self) -> List[str]:
        out = []
        for name in ("point", "tick_size", "tick_value_profit", "tick_value_loss", "volume_min", "volume_step"):
            v = getattr(self, name)
            if not (isinstance(v, (int, float)) and math.isfinite(v) and v > 0):
                out.append(f"{name} must be > 0")
        if self.volume_max < self.volume_min:
            out.append("volume_max < volume_min")
        if self.digits < 0:
            out.append("digits < 0")
        if self.stops_level_points < 0 or self.freeze_level_points < 0:
            out.append("stop/freeze level < 0")
        if self.commission_per_lot < 0 or self.margin_per_lot < 0:
            out.append("commission/margin < 0")
        if self.volume_step > 0 and abs(self.volume_min / self.volume_step - round(self.volume_min / self.volume_step)) > 1e-6:
            out.append("volume_min is not a multiple of volume_step")
        return out


@dataclass
class PlannedPosition:
    lots: float
    risk_money: float


@dataclass
class BasketPlan:
    ok: bool
    reason: str = ""
    direction: int = 0
    entry: float = 0.0
    sl: float = 0.0
    tp: float = 0.0
    stop_distance: float = 0.0
    positions: List[PlannedPosition] = field(default_factory=list)
    total_lots: float = 0.0
    planned_risk_money: float = 0.0     # actual risk after rounding, <= the budget
    target_money: float = 0.0           # planned_risk_money x target multiplier
    margin_required: float = 0.0
    requested_positions: int = 0


def ticks_in(distance: float, tick_size: float) -> float:
    """Distance in ticks; snaps to a whole number when the prices sit on the tick grid (float noise)."""
    t = distance / tick_size
    r = round(t)
    return float(r) if abs(t - r) < 1e-6 else t


def round_down_volume(step: float, lots: float) -> float:
    if lots <= 0:
        return 0.0
    d = (Decimal(str(lots)) / Decimal(str(step))).to_integral_value(rounding=ROUND_FLOOR)
    return float(d * Decimal(str(step)))


def loss_per_lot(spec: SymbolSpec, entry: float, sl: float) -> float:
    return ticks_in(abs(entry - sl), spec.tick_size) * spec.tick_value_loss + spec.commission_per_lot


def position_pnl(spec: SymbolSpec, direction: int, entry: float, price: float, lots: float,
                 include_commission: bool = True) -> float:
    raw = (price - entry) * direction
    ticks = ticks_in(abs(raw), spec.tick_size) * (1 if raw >= 0 else -1)
    pnl = lots * ticks * (spec.tick_value_profit if ticks >= 0 else spec.tick_value_loss)
    if include_commission:
        pnl -= spec.commission_per_lot * lots
    return pnl


def split_weights(n: int, mode: str) -> List[float]:
    if n < 1:
        raise ValueError("n must be >= 1")
    raw = [1.0] * n if mode == "equal" or n == 1 else [FRONT_LOAD_FACTOR] + [1.0] * (n - 1)
    total = sum(raw)
    return [w / total for w in raw]


def target_price(spec: SymbolSpec, direction: int, entry: float, total_lots: float, target_money: float) -> float:
    """Price at which the basket's NET profit (after commission) equals target_money; rounded so it is reached."""
    ticks = (target_money + spec.commission_per_lot * total_lots) / (total_lots * spec.tick_value_profit)
    raw = entry + direction * ticks * spec.tick_size
    q = Decimal(1).scaleb(-spec.digits)
    d = Decimal(str(raw)).quantize(q, rounding=ROUND_CEILING if direction == 1 else ROUND_FLOOR)
    return float(d)


def plan_basket(spec: SymbolSpec, direction: int, entry: float, sl: float, risk_money: float,
                max_positions: int = 1, split_mode: str = "equal", target_multiplier: float = 3.0,
                free_margin: Optional[float] = None, max_lot_per_position: Optional[float] = None) -> BasketPlan:
    def reject(reason):
        return BasketPlan(False, reason, direction, entry, sl, requested_positions=max_positions)

    if direction not in (1, -1):
        raise ValueError("direction must be +1 or -1")
    probs = spec.problems()
    if probs:
        return reject("invalid_symbol_spec: " + "; ".join(probs))
    if not spec.trade_allowed:
        return reject("trading_not_allowed")
    if (direction == 1 and not sl < entry) or (direction == -1 and not sl > entry):
        return reject("sl_wrong_side")
    if not risk_money > 0:
        return reject("no_risk_budget")
    if max_positions < 1:
        return reject("no_positions_allowed")
    dist = abs(entry - sl)
    if spec.stops_level_points and dist < spec.stops_level_points * spec.point:
        return reject("stop_inside_broker_stop_level")
    lpl = loss_per_lot(spec, entry, sl)
    cap = spec.volume_max if max_lot_per_position is None else min(spec.volume_max, max_lot_per_position)
    if cap < spec.volume_min:
        return reject("lot_cap_below_min_volume")
    chosen = None
    for n in range(max_positions, 0, -1):  # as many positions as allowed, fewer if min lot makes it infeasible
        weights = split_weights(n, split_mode)
        lots = [round_down_volume(spec.volume_step, min(risk_money * w / lpl, cap)) for w in weights]
        if all(l >= spec.volume_min for l in lots):
            chosen = (n, weights, lots)
            break
    if chosen is None:
        return reject("min_lot_exceeds_risk")
    n, weights, lots = chosen
    total_lots = sum(lots)
    planned = sum(l * lpl for l in lots)
    margin = total_lots * spec.margin_per_lot
    if free_margin is not None and margin > free_margin:
        return reject("insufficient_margin")
    target_money = planned * target_multiplier
    tp = target_price(spec, direction, entry, total_lots, target_money)
    return BasketPlan(True, "", direction, entry, sl, tp, dist,
                      [PlannedPosition(l, l * lpl) for l in lots], total_lots, planned, target_money, margin, max_positions)
