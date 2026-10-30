"""Basket budget: ONE risk budget per scalp, shared by all positions in the basket."""
from __future__ import annotations

from dataclasses import dataclass

from .config_schema import SystemConfig


@dataclass(frozen=True)
class BasketBudget:
    equity: float
    risk_percent: float
    risk_money: float          # total money at risk for the whole basket
    max_loss_money: float      # basket stop-out limit (never above risk_money)
    target_multiplier: float
    target_money: float        # basket profit target = risk_money * multiplier
    target_percent_of_equity: float


def basket_budget(equity: float, cfg: SystemConfig) -> BasketBudget:
    if not equity > 0:
        raise ValueError("equity must be positive")
    risk_money = equity * cfg.risk.risk_percent / 100.0
    max_loss_pct = cfg.scalp.basket_max_loss_percent or cfg.risk.risk_percent
    return BasketBudget(
        equity=equity,
        risk_percent=cfg.risk.risk_percent,
        risk_money=risk_money,
        max_loss_money=equity * max_loss_pct / 100.0,
        target_multiplier=cfg.scalp.target_multiplier,
        target_money=risk_money * cfg.scalp.target_multiplier,
        target_percent_of_equity=cfg.risk.risk_percent * cfg.scalp.target_multiplier,
    )
