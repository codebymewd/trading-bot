"""Execution reference (Step 5a): the safety gate, basket execution, live management and restart
recovery, written against a small Broker protocol so it can be tested with a simulated broker.
The MQL5 EA (Step 5b) re-implements this logic and must stay behaviour-identical.

Safety posture
- SIGNAL_ONLY (default) runs every check and reports "would trade" but never calls the broker.
- DEMO sends orders only on a demo account. LIVE is not available in this version.
- A setup is marked used BEFORE the first order is sent, so retries/reconnects/restarts cannot duplicate it.
- Stops and targets are attached to each order (broker-side), and state changes only after the broker confirms.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Protocol, Set, Tuple

from . import basket_manager as bm
from .basket import basket_budget
from .config_schema import InstrumentState, Mode, SystemConfig
from .drawdown import clamp_budget, drawdown_state
from .portfolio import portfolio_check
from .propguard import PropState, prop_check
from .sizing import BasketPlan, PlannedPosition, SymbolSpec, loss_per_lot, plan_basket
from .trading_day import DailyCounters, parse_hhmm, resolve_fixed_offset

LIVE_TRADING_AVAILABLE = False          # live execution stays disabled until demo validation is complete
RISK_BREACH_TOLERANCE = 0.10            # actual risk after fills may exceed the budget by at most 10%
MAX_SIGNAL_AGE_SECONDS = 20             # a candidate older than this (after its M5 bar closed) is stale


def open_risk_total(open_baskets) -> float:
    """Worst-case money currently at risk across all open baskets (their stops)."""
    return sum(bm.open_risk_money(b) for b in open_baskets if b.status == "OPEN")


# ------------------------------------------------------------------ broker protocol & value objects
@dataclass
class AccountInfo:
    balance: float
    equity: float
    free_margin: float
    is_demo: bool
    currency: str = "USD"
    login: int = 0              # MT5 account number the terminal is logged into


@dataclass
class OrderResult:
    ok: bool
    retcode: str = ""
    ticket: int = 0
    price: float = 0.0
    lots: float = 0.0
    pnl: float = 0.0          # net result of a close (commission/swap included) when known
    reason: str = ""


@dataclass
class BrokerPosition:
    ticket: int
    symbol: str
    direction: int
    lots: float
    entry: float
    sl: float
    tp: float
    comment: str = ""


class Broker(Protocol):
    def account(self) -> AccountInfo: ...
    def spec(self, symbol: str) -> Optional[SymbolSpec]: ...
    def quote(self, symbol: str) -> Optional[Tuple[float, float]]: ...        # (bid, ask)
    def margin_required(self, symbol: str, direction: int, lots: float, price: float) -> Optional[float]: ...
    def send_market(self, symbol: str, direction: int, lots: float, sl: float, tp: float, comment: str,
                    deviation_points: int) -> OrderResult: ...
    def modify_sl(self, ticket: int, sl: float) -> OrderResult: ...
    def close_position(self, ticket: int, lots: Optional[float] = None) -> OrderResult: ...
    def positions(self) -> List[BrokerPosition]: ...
    def closed_pnl(self, ticket: int) -> Optional[float]: ...                # net result of a position closed by the broker


@dataclass
class Candidate:
    """A CANDIDATE signal from the scalp engine (+ the M5 bar close time)."""
    direction: int
    entry: float
    sl: float
    kind: str
    close_time: int
    key: str                  # stable identity of the setup zone, e.g. "FVG:1000.0:1004.0"


@dataclass
class GateContext:
    cfg: SystemConfig
    now: int
    config_age_seconds: float
    symbol: str
    instrument_state: InstrumentState
    cand: Candidate
    broker: Broker
    store: "BasketStore"
    news_status: str = "unavailable"        # clear | restricted | unavailable   (news engine: step 6)
    volatility: str = "unknown"             # normal | elevated | extreme | unknown
    prop: Optional[PropState] = None
    day_start_equity: Optional[float] = None
    news_extra_spread: bool = False         # set from NewsDecision when inside a news window
    news_extra_vol: bool = False
    market_anomaly: bool = False            # see news.market_anomaly
    recent_closes: Optional[Dict[str, List[float]]] = None   # per-symbol closes for the correlation filter


@dataclass
class GateResult:
    ok: bool
    reason: str = ""
    send_orders: bool = False
    plan: Optional[BasketPlan] = None
    live_entry: float = 0.0


@dataclass
class ExecResult:
    ok: bool
    reason: str = ""
    basket: Optional["bm.Basket"] = None


# ------------------------------------------------------------------ helpers
def volatility_state(atr_now: float, atr_baseline: float, cfg: SystemConfig, symbol: str = "") -> str:
    if not (atr_now > 0 and atr_baseline > 0):
        return "unknown"
    from .instruments import volatility_thresholds
    elevated, extreme = volatility_thresholds(cfg, symbol) if symbol else (cfg.volatility.elevated_ratio, cfg.volatility.extreme_ratio)
    r = atr_now / atr_baseline
    if r >= extreme:
        return "extreme"
    if r >= elevated:
        return "elevated"
    return "normal"


def schedule_allows(now_utc: int, cfg: SystemConfig, server_offset: Optional[int] = None) -> bool:
    off = resolve_fixed_offset(cfg.schedule.timezone)
    if off is None:
        off = server_offset
    if off is None:
        return False                       # unknown timezone offset: do not trade
    minutes = ((now_utc + off) % 86400) // 60
    return parse_hhmm(cfg.schedule.analysis_start) <= minutes < parse_hhmm(cfg.schedule.entry_cutoff)


def setup_id(symbol: str, cand: Candidate) -> str:
    return f"{symbol}|{cand.direction}|{cand.key}|{cand.close_time}"


def _allowed_states(mode: Mode) -> Set[InstrumentState]:
    if mode == Mode.SIGNAL_ONLY:
        return {InstrumentState.MONITORED, InstrumentState.DEMO_ELIGIBLE, InstrumentState.LIVE_VALIDATED}
    if mode == Mode.DEMO:
        return {InstrumentState.DEMO_ELIGIBLE, InstrumentState.LIVE_VALIDATED}
    return {InstrumentState.LIVE_VALIDATED}


# ------------------------------------------------------------------ the gate
def run_gate(c: GateContext) -> GateResult:
    cfg, br, cand = c.cfg, c.broker, c.cand

    def no(reason):
        return GateResult(False, reason)

    if cfg.safety.kill_switch:
        return no("kill_switch")
    if cfg.mode != Mode.SIGNAL_ONLY and br.account().login not in cfg.broker.allowed_logins:
        return no("account_not_allowed")       # trades only on an account the owner explicitly allowed
    if c.config_age_seconds > cfg.safety.stale_config_seconds:
        return no("stale_config")
    if not schedule_allows(c.now, cfg):
        return no("outside_trading_window")
    if c.instrument_state not in _allowed_states(cfg.mode):
        return no("instrument_not_validated_for_mode")
    spec = br.spec(c.symbol)
    if spec is None or spec.problems():
        return no("symbol_spec_unavailable_or_invalid")
    if not spec.trade_allowed:
        return no("trading_not_allowed")
    if c.now - (cand.close_time) > MAX_SIGNAL_AGE_SECONDS:
        return no("stale_signal")
    if setup_id(c.symbol, cand) in c.store.used_setups:
        return no("duplicate_setup")
    if c.news_status == "restricted":
        return no("news_restricted")
    if c.news_status != "clear" and cfg.news.on_data_unavailable == "block":
        return no("news_unavailable")
    if c.market_anomaly:
        return no("market_anomaly")
    if c.volatility not in cfg.volatility.allowed_states:
        return no("volatility_" + c.volatility)
    if c.news_extra_vol and c.volatility != "normal":
        return no("news_extra_volatility")
    q = br.quote(c.symbol)
    if q is None:
        return no("no_quote")
    bid, ask = q
    acct = br.account()
    live_entry = ask if cand.direction == 1 else bid
    risk_dist = abs(cand.entry - cand.sl)
    if risk_dist <= 0:
        return no("zero_risk_distance")
    moved = (live_entry - cand.entry) * cand.direction          # adverse move since the signal bar closed
    if moved > cfg.entry.max_chase_r * risk_dist:
        return no("price_moved_since_signal")
    spread = ask - bid
    spread_limit = cfg.scalp.max_spread_to_stop_ratio * abs(live_entry - cand.sl)
    if c.news_extra_spread:
        spread_limit *= 0.5                   # stricter spread limit while a news window is active
    if spread > spread_limit:
        return no("news_extra_spread" if c.news_extra_spread and spread <= 2 * spread_limit else "spread_too_wide")
    # daily loss limit (own configuration), measured against the start-of-day equity
    if cfg.daily.daily_loss_limit_percent is not None and c.day_start_equity:
        lost = (c.day_start_equity - acct.equity) / c.day_start_equity * 100.0
        if lost >= cfg.daily.daily_loss_limit_percent:
            return no("daily_loss_limit")
    # ---- remaining-drawdown-aware risk --------------------------------------
    # The budget is NEVER simply equity x risk% when a prop profile is active: it is clamped to
    # the drawdown headroom that is actually left (open baskets' worst case already consumes it).
    # Risk is never silently increased; over-capacity is rejected (or reduced only if the user
    # explicitly enabled over_capacity_policy='reduce').
    active_profile = cfg.prop_profiles.get(cfg.active_prop_profile) if cfg.active_prop_profile else None
    dd = drawdown_state(active_profile, balance=acct.balance, equity=acct.equity,
                        day_start_balance=(c.prop.day_start_balance if c.prop else acct.balance),
                        high_water=(c.prop.high_water if c.prop else max(acct.balance, acct.equity)),
                        open_risk_money=open_risk_total(c.store.open_baskets()))
    budget = basket_budget(acct.equity, cfg)
    risk_money, why = clamp_budget(cfg, dd, budget.risk_money)
    if why:
        return no(why)
    plan = plan_basket(spec, cand.direction, live_entry, cand.sl, risk_money,
                       cfg.scalp.max_positions_per_basket, cfg.scalp.risk_split, cfg.scalp.target_multiplier,
                       free_margin=None)
    if not plan.ok:
        return no("plan_rejected:" + plan.reason)
    # exact margin from the broker for the whole basket
    need = br.margin_required(c.symbol, cand.direction, plan.total_lots, live_entry)
    if need is None:
        return no("margin_unavailable")
    if need > acct.free_margin:
        return no("insufficient_margin")
    reason = bm.exposure_check(c.store.open_baskets(), plan, c.symbol, acct.equity, cfg, c.store.counters)
    if reason:
        return no(reason)
    reason = portfolio_check(c.symbol, cand.direction, c.store.open_baskets(), c.recent_closes, cfg.portfolio)
    if reason:
        return no(reason)
    active = cfg.prop_profiles.get(cfg.active_prop_profile) if cfg.active_prop_profile else None
    if active is not None:
        if c.prop is None:
            return no("prop_state_unavailable")
        reason = prop_check(active, c.symbol, c.prop, plan.planned_risk_money, max(p.lots for p in plan.positions))
        if reason:
            return no(reason)
    # execution permission by mode
    if cfg.mode == Mode.SIGNAL_ONLY:
        return GateResult(True, "signal_only", False, plan, live_entry)
    if cfg.mode == Mode.LIVE:
        if not LIVE_TRADING_AVAILABLE:
            return GateResult(False, "live_trading_not_available", False, plan, live_entry)
    if cfg.mode == Mode.DEMO and not acct.is_demo:
        return GateResult(False, "demo_mode_on_live_account", False, plan, live_entry)
    return GateResult(True, "", True, plan, live_entry)


# ------------------------------------------------------------------ persistence
class BasketStore:
    """Baskets, used setup ids, daily counters and an event log; survives restarts (atomic JSON file)."""

    def __init__(self, path: Optional[str] = None):
        self.path = path
        self.baskets: Dict[str, bm.Basket] = {}
        self.used_setups: Set[str] = set()
        self.counters = DailyCounters()
        self.events: List[dict] = []

    def open_baskets(self) -> List[bm.Basket]:
        return [b for b in self.baskets.values() if b.status == "OPEN"]

    def event(self, kind: str, **kw) -> None:
        self.events.append({"type": kind, **kw})

    def save(self) -> None:
        if not self.path:
            return
        data = {"baskets": {k: _basket_to_dict(b) for k, b in self.baskets.items()},
                "used_setups": sorted(self.used_setups), "counters": asdict(self.counters)}
        d = os.path.dirname(os.path.abspath(self.path))
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(data, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise

    @classmethod
    def load(cls, path: str) -> "BasketStore":
        s = cls(path)
        if not os.path.exists(path):
            return s
        try:
            with open(path) as fh:
                data = json.load(fh)
            s.baskets = {k: _basket_from_dict(v) for k, v in data["baskets"].items()}
            s.used_setups = set(data["used_setups"])
            s.counters = DailyCounters(**data["counters"])
        except Exception:
            # unreadable state: fail safe - block trading until a human looks
            s = cls(path)
            s.counters = DailyCounters(locked_out=True, clock_anomaly=True)
            s.event("STATE_CORRUPT")
        return s


def _basket_to_dict(b: bm.Basket) -> dict:
    return asdict(b)


def _basket_from_dict(d: dict) -> bm.Basket:
    d = dict(d)
    d["spec"] = SymbolSpec(**d["spec"])
    d["positions"] = [bm.Position(**p) for p in d["positions"]]
    return bm.Basket(**d)


# ------------------------------------------------------------------ execution
def execute_basket(c: GateContext, gate: GateResult) -> ExecResult:
    """Send the planned positions as market orders. Only call when gate.send_orders is True."""
    if not gate.send_orders or gate.plan is None:
        return ExecResult(False, "orders_not_permitted")
    cfg, br, cand, store, plan = c.cfg, c.broker, c.cand, c.store, gate.plan
    sid = setup_id(c.symbol, cand)
    store.used_setups.add(sid)             # mark BEFORE sending: a retry or restart can never duplicate this setup
    store.save()
    basket_id = f"{c.symbol}-{cand.close_time}-{cand.direction}"
    spec = br.spec(c.symbol)
    acct = br.account()
    budget = basket_budget(acct.equity, cfg)
    fills: List[Tuple[int, float]] = []
    placed: List[Tuple[int, PlannedPosition]] = []
    for i, pp in enumerate(plan.positions):
        res = br.send_market(c.symbol, cand.direction, pp.lots, plan.sl, plan.tp, f"TB|{basket_id}|{i}",
                             cfg.risk.max_slippage_points)
        if not res.ok:
            store.event("ORDER_REJECTED", basket=basket_id, index=i, retcode=res.retcode, reason=res.reason)
            break
        fills.append((res.ticket, res.price))
        placed.append((res.ticket, pp))
    if not fills:
        store.save()
        return ExecResult(False, "first_order_failed")
    # actual risk with the real fill prices (slippage can make it larger than planned)
    actual = sum(pp.lots * loss_per_lot(spec, price, plan.sl) for (_, price), (_, pp) in zip(fills, placed))
    if actual > budget.risk_money * (1.0 + RISK_BREACH_TOLERANCE):
        for ticket, _ in fills:
            br.close_position(ticket)
        store.event("SLIPPAGE_RISK_BREACH", basket=basket_id, actual=actual, budget=budget.risk_money)
        store.save()
        return ExecResult(False, "slippage_risk_breach")
    sub = BasketPlan(True, "", plan.direction, plan.entry, plan.sl, plan.tp, plan.stop_distance,
                     [p for _, p in placed], sum(p.lots for _, p in placed), actual, actual * cfg.scalp.target_multiplier,
                     plan.margin_required, plan.requested_positions)
    basket = bm.open_basket(basket_id, spec, sub, fills, c.now, cfg.scalp.scalp_window_minutes * 60, budget.max_loss_money)
    store.baskets[basket_id] = basket
    store.counters.on_basket_opened()
    if len(fills) < len(plan.positions):
        store.event("BASKET_PARTIAL", basket=basket_id, opened=len(fills), planned=len(plan.positions))
    store.event("BASKET_OPENED", basket=basket_id, symbol=c.symbol, direction=cand.direction,
                lots=basket.total_lots, risk=actual, sl=plan.sl, tp=plan.tp)
    store.save()
    return ExecResult(True, "", basket)



# ------------------------------------------------------------------ management
def _sync_with_broker(b: bm.Basket, broker: Broker, store: "BasketStore", cfg: SystemConfig) -> None:
    """Positions that vanished were closed by their broker-side stop/target (or manually)."""
    live = {p.ticket for p in broker.positions()}
    for pos in list(b.positions):
        if pos.ticket not in live:
            pnl = broker.closed_pnl(pos.ticket)
            b.realized += pnl if pnl is not None else 0.0
            b.positions.remove(pos)
            store.event("POSITION_CLOSED_BY_BROKER", basket=b.id, ticket=pos.ticket, pnl=pnl)
    if b.status == "OPEN" and not b.positions:
        _finalize(b, b.realized, "stop_or_target", store, cfg)


def _finalize(b: bm.Basket, net: float, reason: str, store: "BasketStore", cfg: SystemConfig) -> None:
    bm.confirm_closed(b, net, reason)
    limit_money = None
    outcome = store.counters.on_basket_closed(
        net, cfg.daily.be_tolerance_money, cfg.daily.lockout_after_losses, cfg.daily.max_consecutive_losses, limit_money)
    store.event("BASKET_CLOSED", basket=b.id, reason=reason, net=net, outcome=outcome)
    if store.counters.locked_out:
        store.event("LOCKOUT_ACTIVE", basket=b.id)


def manage_basket(b: bm.Basket, broker: Broker, params: bm.ManagerParams, now: int, store: "BasketStore",
                  cfg: SystemConfig, structural_sl: Optional[float] = None) -> List[bm.Action]:
    if b.status != "OPEN":
        return []
    _sync_with_broker(b, broker, store, cfg)
    if b.status != "OPEN":
        store.save()
        return []
    q = broker.quote(b.spec.symbol)
    if q is None:
        store.event("NO_QUOTE_FOR_MANAGEMENT", basket=b.id)
        return []
    acts = bm.evaluate(b, now, q[0], q[1], params, structural_sl)
    bm.apply_internal(b, acts)
    for a in acts:
        if a.kind == "CLOSE_ALL":
            for pos in list(b.positions):
                res = broker.close_position(pos.ticket)
                if res.ok:
                    b.realized += res.pnl
                    b.positions.remove(pos)
                else:
                    store.event("CLOSE_FAILED", basket=b.id, ticket=pos.ticket, retcode=res.retcode)
            if not b.positions:
                _finalize(b, b.realized, a.reason, store, cfg)
        elif a.kind == "PARTIAL_CLOSE":
            for ticket, lots in a.detail["lots"].items():
                res = broker.close_position(ticket, lots)
                if res.ok:
                    bm.confirm_partial(b, ticket, lots, res.pnl)
                else:
                    store.event("PARTIAL_CLOSE_FAILED", basket=b.id, ticket=ticket, retcode=res.retcode)
        elif a.kind == "MODIFY_SL":
            done: Dict[int, float] = {}
            for ticket, sl in a.detail["sl"].items():
                res = broker.modify_sl(ticket, sl)
                if res.ok:
                    done[ticket] = sl
                else:
                    store.event("SL_MODIFY_FAILED", basket=b.id, ticket=ticket, sl=sl, retcode=res.retcode)
            if done:
                bm.confirm_sl_moved(b, done, timeout=(a.reason == "timeout_breakeven"))
                store.event("SL_MOVED", basket=b.id, reason=a.reason, moved=len(done), requested=len(a.detail["sl"]))
    store.save()
    return acts


# ------------------------------------------------------------------ recovery after a restart / disconnect
def recover(store: "BasketStore", broker: Broker, cfg: SystemConfig) -> None:
    for b in list(store.open_baskets()):
        _sync_with_broker(b, broker, store, cfg)
    known = {pos.ticket for b in store.open_baskets() for pos in b.positions}
    for p in broker.positions():
        if p.ticket in known:
            continue
        parts = p.comment.split("|")
        if len(parts) == 3 and parts[0] == "TB" and parts[1] in store.baskets and store.baskets[parts[1]].status == "OPEN":
            continue
        store.event("UNKNOWN_POSITION", ticket=p.ticket, symbol=p.symbol, comment=p.comment)  # alert; never auto-managed
    store.save()
