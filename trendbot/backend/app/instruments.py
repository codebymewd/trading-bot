"""Instrument intelligence: spec validation, validation lifecycle, asset-class defaults and setup ranking.
An unknown, unsupported or unvalidated instrument defaults to NO TRADE; names alone never imply understanding."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .config_schema import InstrumentState, SystemConfig
from .news import asset_class_of
from .portfolio import exposure_vector, rolling_correlation, shared_exposure
from .sizing import SymbolSpec

SUPPORTED_ASSET_CLASSES = {"forex", "metals", "crypto"}      # others are monitored only (no asset-class rules implemented)
MIN_HISTORY_BARS = {"D1": 120, "H4": 300, "M15": 1000, "M5": 3000}
MIN_DEMO_DAYS = 14

# Starting values only, NOT calibrated results: crypto and metals are allowed wider volatility than forex.
ASSET_CLASS_VOLATILITY = {"forex": (1.4, 2.2), "metals": (1.5, 2.5), "crypto": (2.0, 3.5)}


@dataclass
class Validation:
    ok: bool
    reasons: List[str] = field(default_factory=list)


def validate_instrument(spec: Optional[SymbolSpec], bars_available: Dict[str, int], asset_class: str) -> Validation:
    if spec is None:
        return Validation(False, ["no_specification_from_terminal"])
    reasons = list(spec.problems())
    if not spec.trade_allowed:
        reasons.append("trading_not_allowed")
    if asset_class not in SUPPORTED_ASSET_CLASSES:
        reasons.append(f"asset_class_not_supported:{asset_class}")
    for tf, need in MIN_HISTORY_BARS.items():
        if bars_available.get(tf, 0) < need:
            reasons.append(f"insufficient_history_{tf}({bars_available.get(tf, 0)}<{need})")
    return Validation(not reasons, reasons)


def promote(current: InstrumentState, target: InstrumentState, evidence: Dict) -> Tuple[bool, str]:
    """Moves along DISCOVERED -> SPEC_VALIDATED -> MONITORED -> DEMO_ELIGIBLE -> LIVE_VALIDATED only with evidence."""
    order = [InstrumentState.DISCOVERED, InstrumentState.SPEC_VALIDATED, InstrumentState.MONITORED,
             InstrumentState.DEMO_ELIGIBLE, InstrumentState.LIVE_VALIDATED]
    if target in (InstrumentState.TEMP_BLOCKED, InstrumentState.USER_DISABLED):
        return True, ""                                       # blocking is always allowed
    if current in (InstrumentState.TEMP_BLOCKED, InstrumentState.USER_DISABLED):
        return False, "unblock_first"
    if order.index(target) != order.index(current) + 1:
        return False, "promotion_must_go_one_step_at_a_time"
    if target == InstrumentState.SPEC_VALIDATED and not evidence.get("spec_ok"):
        return False, "specification_not_validated"
    if target == InstrumentState.MONITORED and not evidence.get("history_ok"):
        return False, "insufficient_history"
    if target == InstrumentState.DEMO_ELIGIBLE and not (evidence.get("oos_backtest_reviewed") and evidence.get("user_confirmed")):
        return False, "needs_out_of_sample_review_and_user_confirmation"
    if target == InstrumentState.LIVE_VALIDATED and not (evidence.get("demo_days", 0) >= MIN_DEMO_DAYS and
                                                         evidence.get("demo_rule_violations", 1) == 0 and evidence.get("user_confirmed")):
        return False, f"needs_{MIN_DEMO_DAYS}_clean_demo_days_and_user_confirmation"
    return True, ""


def volatility_thresholds(cfg: SystemConfig, symbol: str) -> Tuple[float, float]:
    """(elevated, extreme) ratios: per-asset-class override from the config, else the global values."""
    cls = asset_class_of(symbol)
    for key, params in cfg.asset_class_params.items():
        if key.value == cls and params.volatility is not None:
            return params.volatility.elevated_ratio, params.volatility.extreme_ratio
    return cfg.volatility.elevated_ratio, cfg.volatility.extreme_ratio


@dataclass
class ScoredCandidate:
    symbol: str
    direction: int
    kind: str                       # FVG | SWEEP
    confirmations: int              # indicator confirmations achieved
    confirmations_enabled: int
    bos_linked: bool = False
    sweep_present: bool = False
    spread_to_stop: float = 0.0     # spread / stop distance
    cost_to_risk: float = 0.0       # (spread + commission) / risk money
    volatility: str = "normal"
    closes: Optional[Sequence[float]] = None


def score(c: ScoredCandidate) -> float:
    """0..100. Documented weights; deliberately ignores recent candle size, volatility and past returns."""
    conf = c.confirmations / c.confirmations_enabled if c.confirmations_enabled else 1.0
    s = 35 * conf + 15 * c.bos_linked + 10 * c.sweep_present + 10 * (c.kind == "FVG")
    s += 15 * max(0.0, 1.0 - c.spread_to_stop / 0.15) + 10 * max(0.0, 1.0 - c.cost_to_risk / 0.15)
    s += 5 * (c.volatility == "normal")
    return round(s, 2)


def select(cands: Sequence[ScoredCandidate], slots: int, policy: str, corr_threshold: float = 0.8,
           lookback: int = 100) -> List[ScoredCandidate]:
    """Choose up to `slots` setups: rank by the policy, skipping any that overlap (shared currency or high
    correlation) with one already chosen. Everything else is skipped, not queued."""
    if policy == "lowest_cost":
        ranked = sorted(cands, key=lambda c: (c.cost_to_risk, -score(c)))
    elif policy == "least_correlated":
        ranked = sorted(cands, key=lambda c: -score(c))
    else:
        ranked = sorted(cands, key=lambda c: -score(c))
    chosen: List[ScoredCandidate] = []
    for c in ranked:
        if len(chosen) >= slots:
            break
        ok = True
        for k in chosen:
            if shared_exposure(exposure_vector(c.symbol, c.direction), exposure_vector(k.symbol, k.direction)):
                ok = False
            elif c.closes is not None and k.closes is not None:
                corr = rolling_correlation(c.closes, k.closes, lookback)
                if corr is not None and corr * c.direction * k.direction >= corr_threshold:
                    ok = False
            if not ok:
                break
        if ok:
            chosen.append(c)
    return chosen
