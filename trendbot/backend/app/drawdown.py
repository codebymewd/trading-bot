"""Remaining-drawdown-aware prop-firm risk (integrates directly with the risk engine).

The old sizing used `equity x risk_percent` unconditionally. That is wrong when a fraction of the
permitted drawdown has already been consumed. This module computes the true picture and CLAMPS the
next trade's risk to what the remaining drawdown can actually absorb - it never silently increases
the configured risk, and it BLOCKS when the headroom cannot absorb the planned risk (policy 'reject')
or reduces it only when the user explicitly opted into policy 'reduce'.

Distinguishes:
  initial balance / current balance / current equity / floating P/L /
  current drawdown / maximum allowed drawdown / remaining drawdown /
  daily loss used / daily loss remaining / current open risk / next trade risk

Drawdown models supported (per PropProfile):
  drawdown_type  static   -> limit measured from the starting balance
                 trailing -> high-water mark: max(starting_balance, peak equity/balance);
                             for FTMO-style trailing-to-initial the floor is the starting balance
  drawdown_basis balance  -> realised balance decides the drawdown level
                 equity   -> floating P/L included
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from .config_schema import PropProfile, SystemConfig


@dataclass(frozen=True)
class DrawdownState:
    """Everything the dashboard and the risk engine need, in account currency."""
    initial_balance: float
    balance: float
    equity: float
    floating_pl: float
    max_drawdown_money: float          # maximum permitted overall loss (absolute $)
    current_drawdown_money: float      # drawdown CONSUMED so far (realised + floating per basis)
    remaining_drawdown_money: float    # max_drawdown - current_drawdown  (>= 0)
    daily_loss_limit_money: Optional[float]
    daily_loss_used_money: float
    daily_loss_remaining_money: Optional[float]
    open_risk_money: float             # worst-case loss of all currently open baskets
    next_trade_risk_money: float       # headroom left for ONE more basket (inf = no prop profile)
    blocked_reason: str = ""           # '' when a trade may proceed


def _hwm(profile: PropProfile, balance: float, equity: float, high_water: float) -> float:
    """High-water mark reference for trailing drawdown (never below the starting balance)."""
    cur = equity if profile.drawdown_basis == "equity" else balance
    return max(profile.starting_balance, high_water, cur)


def drawdown_state(profile: Optional[PropProfile], *, balance: float, equity: float,
                   day_start_balance: float, high_water: float,
                   open_risk_money: float = 0.0) -> DrawdownState:
    """Compute the full prop-firm picture. `open_risk_money` is the worst-case loss of all
    currently open baskets (their stops). next_trade_risk_money = remaining headroom for one
    more basket after that open risk and the safety buffer are accounted for."""
    floating = equity - balance
    if profile is None:
        # no prop profile -> no drawdown cap at all (inf, NOT 0: a zero here must never block)
        return DrawdownState(
            initial_balance=balance, balance=balance, equity=equity, floating_pl=floating,
            max_drawdown_money=math.inf, current_drawdown_money=0.0, remaining_drawdown_money=math.inf,
            daily_loss_limit_money=None, daily_loss_used_money=0.0, daily_loss_remaining_money=None,
            open_risk_money=open_risk_money, next_trade_risk_money=math.inf)
    if not profile.confirmed or profile.unresolved_fields:
        # an incomplete profile must fail safe: no usable headroom at all
        return DrawdownState(
            initial_balance=profile.starting_balance, balance=balance, equity=equity, floating_pl=floating,
            max_drawdown_money=0.0, current_drawdown_money=0.0, remaining_drawdown_money=0.0,
            daily_loss_limit_money=None, daily_loss_used_money=0.0, daily_loss_remaining_money=None,
            open_risk_money=open_risk_money, next_trade_risk_money=0.0,
            blocked_reason="prop_profile_not_ready")

    start = profile.starting_balance
    buf_frac = profile.safety_buffer_percent / 100.0

    # ---- overall drawdown -------------------------------------------------
    max_dd_money = start * profile.max_drawdown_percent / 100.0 if profile.max_drawdown_percent is not None else math.inf
    if profile.drawdown_type == "trailing":
        ref = _hwm(profile, balance, equity, high_water)
    else:
        ref = start
    measure = equity if profile.drawdown_basis == "equity" else balance
    used = max(ref - measure, 0.0)
    remaining = max(max_dd_money - used, 0.0) if math.isfinite(max_dd_money) else math.inf
    usable_overall = max(remaining - start * buf_frac, 0.0) if math.isfinite(remaining) else math.inf

    # ---- daily drawdown ---------------------------------------------------
    daily_limit_money = None
    daily_used = 0.0
    daily_remaining = None
    usable_daily = math.inf
    if profile.daily_loss_limit_percent is not None:
        base = start if profile.daily_loss_reference == "starting_balance" else day_start_balance
        daily_limit_money = base * profile.daily_loss_limit_percent / 100.0
        cur_daily = equity if profile.count_floating_pl else balance
        daily_used = max(base - cur_daily, 0.0)
        daily_remaining = max(daily_limit_money - daily_used, 0.0)
        usable_daily = max(daily_remaining - base * buf_frac, 0.0)

    # ---- headroom available to the NEXT trade ------------------------------
    cap = min(x for x in (usable_overall, usable_daily) if x is not None)
    headroom = max(cap - open_risk_money, 0.0)      # open baskets' worst case already consumes cap

    return DrawdownState(
        initial_balance=start, balance=balance, equity=equity, floating_pl=floating,
        max_drawdown_money=max_dd_money, current_drawdown_money=used, remaining_drawdown_money=remaining,
        daily_loss_limit_money=daily_limit_money, daily_loss_used_money=daily_used,
        daily_loss_remaining_money=daily_remaining,
        open_risk_money=open_risk_money, next_trade_risk_money=headroom,
        blocked_reason="")


def clamp_budget(cfg: SystemConfig, state: DrawdownState, budget_risk_money: float) -> tuple:
    """Return (risk_money, reason). Risk is NEVER increased above the configured budget;
    it is reduced to the remaining-drawdown headroom, or BLOCKED outright when the policy is
    'reject' (the default) and the headroom cannot absorb the full planned risk. Silently
    reducing is only allowed when the user explicitly set over_capacity_policy='reduce'."""
    if state.blocked_reason:
        return 0.0, state.blocked_reason
    if math.isfinite(state.remaining_drawdown_money) and state.remaining_drawdown_money <= 0:
        return 0.0, "prop_remaining_drawdown"
    headroom = state.next_trade_risk_money
    if budget_risk_money > headroom:
        if cfg.risk.over_capacity_policy != "reduce":
            return 0.0, "risk_exceeds_remaining_drawdown"     # BLOCK - never silently increase, never silently cut
        risk = min(budget_risk_money, headroom)
        if risk <= 0:
            return 0.0, "no_drawdown_headroom"
        return risk, ""
    return budget_risk_money, ""
