"""Prop-firm guard: checked before EVERY order (the EA re-implements this independently).
Rules are only as good as the confirmed profile; this does not guarantee compliance with a firm's full terms.
Not enforced yet: overnight/weekend/consistency/minimum-day rules and news-window rules (documented limits).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .config_schema import PropProfile


@dataclass
class PropState:
    balance: float
    equity: float
    day_start_balance: float
    high_water: float          # highest equity/balance (per profile basis) since the account started


def prop_check(profile: Optional[PropProfile], symbol: str, state: PropState,
               planned_risk_money: float, max_position_lots: float) -> str:
    """'' when allowed, else a reason code. Includes the new basket's worst-case loss (its stop)."""
    if profile is None:
        return ""
    if not profile.confirmed or profile.unresolved_fields:
        return "prop_profile_not_ready"
    if symbol.lower() in {s.lower() for s in profile.restricted_symbols}:
        return "prop_symbol_restricted"
    if profile.max_lot_size is not None and max_position_lots > profile.max_lot_size:
        return "prop_lot_limit"
    current = state.equity if profile.count_floating_pl else state.balance
    buf = profile.safety_buffer_percent
    if profile.daily_loss_limit_percent is not None:
        base = profile.starting_balance if profile.daily_loss_reference == "starting_balance" else state.day_start_balance
        used = max(state.day_start_balance - current, 0.0) / base * 100.0
        if used + planned_risk_money / base * 100.0 + buf > profile.daily_loss_limit_percent:
            return "prop_daily_loss"
    if profile.max_drawdown_percent is not None:
        ref = profile.starting_balance if profile.drawdown_type == "static" else max(state.high_water, profile.starting_balance)
        used = max(ref - (state.equity if profile.drawdown_basis == "equity" else state.balance), 0.0)
        used_pct = used / profile.starting_balance * 100.0
        if used_pct + planned_risk_money / profile.starting_balance * 100.0 + buf > profile.max_drawdown_percent:
            return "prop_drawdown"
    return ""
