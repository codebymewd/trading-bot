"""Basket lifecycle rules (reference for the EA port): target, max loss, timeout, break-even,
exposure checks. `evaluate` is PURE: it returns actions. Basket state changes only through the
confirm_* functions, which the EA calls AFTER the broker accepted the request, so a failed stop
modification is never reported as done.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Dict, List, Optional

from .config_schema import SystemConfig
from .sizing import BasketPlan, SymbolSpec, loss_per_lot, position_pnl, round_down_volume


@dataclass
class Position:
    ticket: int
    lots: float
    entry: float
    sl: float
    tp: float = 0.0


@dataclass
class Basket:
    id: str
    spec: SymbolSpec
    direction: int
    opened_at: int                 # epoch seconds
    window_seconds: int
    stop_distance: float
    planned_risk_money: float
    target_money: float
    max_loss_money: float
    positions: List[Position] = field(default_factory=list)
    realized: float = 0.0          # net P/L already booked by partial closes
    status: str = "OPEN"           # OPEN | CLOSED
    close_reason: str = ""
    final_pnl: float = 0.0
    be_done: bool = False
    partial_done: bool = False
    timeout_handled: bool = False

    @property
    def total_lots(self) -> float:
        return sum(p.lots for p in self.positions)

    @property
    def avg_entry(self) -> float:
        tl = self.total_lots
        return sum(p.entry * p.lots for p in self.positions) / tl if tl else 0.0


@dataclass
class Action:
    kind: str                      # CLOSE_ALL | MODIFY_SL | PARTIAL_CLOSE | MARK_TIMEOUT (internal)
    reason: str
    detail: Dict = field(default_factory=dict)


@dataclass
class ManagerParams:
    break_even_method: str = "off"      # off | A | B | C
    trigger_r: float = 1.0
    be_offset_points: int = 0
    partial_close_percent: float = 0.0
    timeout_action: str = "CLOSE_ALL"


def params_from_config(cfg: SystemConfig) -> ManagerParams:
    return ManagerParams(cfg.break_even.method, cfg.break_even.trigger_r, cfg.break_even.be_offset_points,
                         cfg.break_even.partial_close_percent, cfg.scalp.timeout_action)


def open_basket(basket_id: str, spec: SymbolSpec, plan: BasketPlan, fills, opened_at: int,
                window_seconds: int, max_loss_money: float) -> Basket:
    """fills: [(ticket, fill_price)] aligned with plan.positions. Stops/targets come from the plan."""
    if not plan.ok:
        raise ValueError("cannot open a rejected plan")
    if len(fills) != len(plan.positions):
        raise ValueError("fills must match the planned positions")
    b = Basket(basket_id, spec, plan.direction, opened_at, window_seconds, plan.stop_distance,
               plan.planned_risk_money, plan.target_money, max_loss_money)
    for (ticket, price), pp in zip(fills, plan.positions):
        b.positions.append(Position(ticket, pp.lots, price, plan.sl, plan.tp))
    return b


# ------------------------------------------------------------------ P/L
def exit_price(b: Basket, bid: float, ask: float) -> float:
    return bid if b.direction == 1 else ask  # long closes at bid, short closes at ask


def basket_pnl(b: Basket, bid: float, ask: float) -> float:
    price = exit_price(b, bid, ask)
    return b.realized + sum(position_pnl(b.spec, b.direction, p.entry, price, p.lots) for p in b.positions)


def open_risk_money(b: Basket) -> float:
    """Money still at risk at the CURRENT stops (0 once the stop is at/over break-even)."""
    total = 0.0
    for p in b.positions:
        on_loss_side = (p.sl < p.entry) if b.direction == 1 else (p.sl > p.entry)
        if on_loss_side:
            total += p.lots * loss_per_lot(b.spec, p.entry, p.sl)
    return total


# ------------------------------------------------------------------ rules
def _round_price(spec: SymbolSpec, price: float) -> float:
    return float(Decimal(str(price)).quantize(Decimal(1).scaleb(-spec.digits), rounding=ROUND_HALF_UP))


def _sl_valid(b: Basket, new_sl: float, bid: float, ask: float) -> Dict[int, float]:
    """Positions whose stop can be TIGHTENED to new_sl right now (never loosened, broker stop level respected)."""
    s = b.spec
    min_dist = s.stops_level_points * s.point
    ok: Dict[int, float] = {}
    for p in b.positions:
        if b.direction == 1:
            tighter = new_sl > p.sl
            far_enough = (bid - new_sl) >= min_dist and new_sl < bid
        else:
            tighter = new_sl < p.sl
            far_enough = (new_sl - ask) >= min_dist and new_sl > ask
        if tighter and far_enough:
            ok[p.ticket] = new_sl
    return ok


def _be_price(b: Basket, p: ManagerParams) -> float:
    off = p.be_offset_points * b.spec.point
    return _round_price(b.spec, b.avg_entry + b.direction * off)


def _favorable_move(b: Basket, bid: float, ask: float) -> float:
    return (bid - b.avg_entry) if b.direction == 1 else (b.avg_entry - ask)


def _partial_actions(b: Basket, p: ManagerParams) -> Dict[int, float]:
    out: Dict[int, float] = {}
    if p.break_even_method != "B" or p.partial_close_percent <= 0 or b.partial_done:
        return out
    for pos in b.positions:
        close = round_down_volume(b.spec.volume_step, pos.lots * p.partial_close_percent / 100.0)
        if close > 0 and pos.lots - close >= b.spec.volume_min:
            out[pos.ticket] = close
    return out


def evaluate(b: Basket, now: int, bid: float, ask: float, p: ManagerParams,
             structural_sl: Optional[float] = None) -> List[Action]:
    if b.status != "OPEN":
        return []
    pnl = basket_pnl(b, bid, ask)
    if pnl <= -b.max_loss_money:
        return [Action("CLOSE_ALL", "max_loss", {"pnl": pnl})]
    if pnl >= b.target_money:
        return [Action("CLOSE_ALL", "target", {"pnl": pnl})]
    if not b.timeout_handled and now - b.opened_at >= b.window_seconds:
        a = p.timeout_action
        if a == "CLOSE_ALL":
            return [Action("CLOSE_ALL", "timeout", {"pnl": pnl})]
        if a == "CLOSE_IF_LOSS" and pnl < 0:
            return [Action("CLOSE_ALL", "timeout_loss", {"pnl": pnl})]
        if a == "MOVE_TO_BREAKEVEN" and not b.be_done:
            moves = _sl_valid(b, _be_price(b, p), bid, ask)
            if moves:
                return [Action("MODIFY_SL", "timeout_breakeven", {"sl": moves})]
        return [Action("MARK_TIMEOUT", f"timeout_{a.lower()}_no_action")]  # keep running with current stops
    if p.break_even_method != "off" and not b.be_done:
        if _favorable_move(b, bid, ask) >= p.trigger_r * b.stop_distance:
            target = structural_sl if p.break_even_method == "C" else _be_price(b, p)
            if target is not None:
                moves = _sl_valid(b, _round_price(b.spec, target), bid, ask)
                if moves:
                    acts: List[Action] = []
                    part = _partial_actions(b, p)
                    if part:
                        acts.append(Action("PARTIAL_CLOSE", "break_even_partial", {"lots": part}))
                    acts.append(Action("MODIFY_SL", "break_even", {"sl": moves}))
                    return acts
    return []


def apply_internal(b: Basket, actions: List[Action]) -> None:
    for a in actions:
        if a.kind == "MARK_TIMEOUT":
            b.timeout_handled = True


# ------------------------------------------------------------------ confirmations from the broker
def confirm_sl_moved(b: Basket, new_sls: Dict[int, float], timeout: bool = False) -> None:
    for p in b.positions:
        if p.ticket in new_sls:
            p.sl = new_sls[p.ticket]
    b.be_done = True
    if timeout:
        b.timeout_handled = True


def confirm_partial(b: Basket, ticket: int, lots_closed: float, realized_pnl: float) -> None:
    for p in b.positions:
        if p.ticket == ticket:
            p.lots = round(p.lots - lots_closed, 8)
    b.realized += realized_pnl
    b.partial_done = True


def confirm_closed(b: Basket, net_pnl: float, reason: str) -> None:
    b.status = "CLOSED"
    b.close_reason = reason
    b.final_pnl = net_pnl
    b.positions = []


# ------------------------------------------------------------------ exposure
def can_add_position(b: Basket) -> bool:
    """Baskets are opened in one go from the plan; nothing may be added later (no martingale/averaging)."""
    return False


def exposure_check(open_baskets: List[Basket], new_plan: BasketPlan, symbol: str, equity: float,
                   cfg: SystemConfig, counters) -> str:
    """Returns '' when a new basket may open, else a reason code."""
    if counters.locked_out:
        return "locked_out"
    if counters.clock_anomaly:
        return "clock_anomaly"
    if counters.trades_opened >= cfg.daily.max_trades_per_day:
        return "daily_basket_limit"
    live = [x for x in open_baskets if x.status == "OPEN"]
    if any(x.spec.symbol == symbol for x in live):
        return "symbol_already_open"
    if sum(len(x.positions) for x in live) + len(new_plan.positions) > cfg.risk.max_simultaneous_positions:
        return "position_cap"
    cap = equity * cfg.risk.max_open_risk_percent / 100.0
    if sum(open_risk_money(x) for x in live) + new_plan.planned_risk_money > cap + 1e-9:
        return "aggregate_risk_cap"
    return ""
