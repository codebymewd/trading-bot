"""Authoritative, versioned configuration schema for TrendBot.

The backend validates every change against this schema; the EA re-validates the
same rules independently (the EA is the final authority).
"""
from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .trading_day import TimezoneError, resolve_fixed_offset

SCHEMA_VERSION = 3

TF_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60}
TARGET_MULTIPLIER_MIN, TARGET_MULTIPLIER_MAX = 0.5, 10.0
SCALP_WINDOW_MIN, SCALP_WINDOW_MAX = 1, 240
BASKET_POSITIONS_MAX = 10
# Telegram quick-pick presets (the dashboard also allows a validated custom value)
TARGET_PRESETS = (1.0, 2.0, 3.0, 4.0, 5.0)
WINDOW_PRESETS = (5, 15, 30)
BE_TRIGGER_PRESETS = (0.5, 1.0, 1.5)
IndicatorName = Literal["MACD", "STOCH", "BB", "RSI", "CCI"]
TimeoutAction = Literal["CLOSE_ALL", "MOVE_TO_BREAKEVEN", "KEEP_RUNNING", "CLOSE_IF_LOSS"]


def _check_timezone(value: str) -> str:
    try:
        resolve_fixed_offset(value)
    except TimezoneError as exc:
        raise ValueError(str(exc)) from exc
    return value


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class Mode(str, Enum):
    SIGNAL_ONLY = "SIGNAL_ONLY"
    DEMO = "DEMO"
    LIVE = "LIVE"


class AssetClass(str, Enum):
    FOREX = "forex"
    METALS = "metals"
    CRYPTO = "crypto"
    INDICES = "indices"
    ENERGIES = "energies"
    STOCKS = "stocks"
    OTHER = "other"


class InstrumentState(str, Enum):
    DISCOVERED = "DISCOVERED"
    SPEC_VALIDATED = "SPEC_VALIDATED"
    MONITORED = "MONITORED"
    DEMO_ELIGIBLE = "DEMO_ELIGIBLE"
    LIVE_VALIDATED = "LIVE_VALIDATED"
    TEMP_BLOCKED = "TEMP_BLOCKED"
    USER_DISABLED = "USER_DISABLED"


# --------------------------------------------------------------------- risk
class RiskConfig(Strict):
    risk_percent: float = Field(1.0, gt=0, description="% of equity risked per trade")
    over_capacity_policy: Literal["reject", "reduce"] = "reject"  # reduce only if explicitly enabled
    include_commission_in_risk: bool = True
    max_open_risk_percent: float = Field(1.0, gt=0)
    max_simultaneous_positions: int = Field(1, ge=1)
    max_slippage_points: int = Field(30, ge=0)


class BreakEvenConfig(Strict):
    method: Literal["off", "A", "B", "C"] = "off"  # OFF unless explicitly enabled
    trigger_r: float = Field(1.0, gt=0, le=10)
    partial_close_percent: float = Field(0.0, ge=0, lt=100)  # used by B only; disabled by default
    be_offset_points: int = Field(0, ge=0)
    trailing_enabled: bool = False
    trailing_atr_mult: float = Field(1.5, gt=0)

    @model_validator(mode="after")
    def _check(self):
        if self.method != "B" and self.partial_close_percent > 0:
            raise ValueError("partial_close_percent requires method 'B'")
        return self


class DailyLimits(Strict):
    max_trades_per_day: int = Field(2, ge=1)  # counted in units of count_unit
    count_unit: Literal["basket", "position"] = "basket"
    lockout_after_losses: int = Field(2, ge=1)
    max_consecutive_losses: Optional[int] = Field(None, ge=1)
    daily_loss_limit_percent: Optional[float] = Field(None, gt=0)
    be_tolerance_money: float = Field(0.0, ge=0)  # |net| <= this counts as neutral
    reset_time: str = Field("00:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    reset_timezone: str = "Africa/Lagos"  # WAT, UTC+1, no DST; or 'broker_server'

    _tz = field_validator("reset_timezone")(lambda cls, v: _check_timezone(v))


class ScheduleConfig(Strict):
    timezone: str = "Africa/Lagos"
    analysis_start: str = Field("00:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    entry_cutoff: str = Field("21:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    forced_close_time: Optional[str] = Field(None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    manage_positions_after_cutoff: bool = True

    _tz = field_validator("timezone")(lambda cls, v: _check_timezone(v))


# --------------------------------------------------------------------- news
class NewsImpactPolicy(Strict):
    allow_entries: bool = True
    pre_window_min: int = Field(0, ge=0)
    post_window_min: int = Field(0, ge=0)
    extra_spread_check: bool = False
    extra_volatility_check: bool = False


class NewsConfig(Strict):
    low: NewsImpactPolicy = NewsImpactPolicy()
    medium: NewsImpactPolicy = NewsImpactPolicy(extra_spread_check=True, extra_volatility_check=True,
                                                pre_window_min=5, post_window_min=5)
    high: NewsImpactPolicy = NewsImpactPolicy(allow_entries=False, pre_window_min=30, post_window_min=15,
                                              extra_spread_check=True, extra_volatility_check=True)
    permitted_high_impact_event_types: List[str] = []
    hold_positions_through_news: bool = True
    close_before_high_impact: bool = False  # prop-firm rule may force this
    data_stale_after_minutes: int = Field(60, ge=1)
    on_data_unavailable: Literal["block", "allow"] = "block"  # fail-safe default


# --------------------------------------------------------------- volatility
class VolatilityConfig(Strict):
    atr_period: int = Field(14, ge=2)
    baseline_lookback_bars: int = Field(200, ge=20)
    elevated_ratio: float = Field(1.5, gt=1)  # ATR / baseline ATR
    extreme_ratio: float = Field(2.5, gt=1)
    allowed_states: List[Literal["normal", "elevated"]] = ["normal", "elevated"]
    max_spread_atr_ratio: float = Field(0.15, gt=0)

    @model_validator(mode="after")
    def _check(self):
        if self.extreme_ratio <= self.elevated_ratio:
            raise ValueError("extreme_ratio must exceed elevated_ratio")
        return self


class PortfolioConfig(Strict):
    correlation_lookback_bars: int = Field(100, ge=20)
    correlation_block_threshold: float = Field(0.8, gt=0, le=1)
    block_on_shared_currency_exposure: bool = True


# ---------------------------------------------------------------- strategy
class IndicatorParams(Strict):
    rsi_period: int = Field(14, ge=2, le=100)
    rsi_midline: float = Field(50.0, ge=30, le=70)
    macd_fast: int = Field(12, ge=2, le=100)
    macd_slow: int = Field(26, ge=3, le=200)
    macd_signal: int = Field(9, ge=2, le=100)
    stoch_k: int = Field(14, ge=2, le=100)
    stoch_d: int = Field(3, ge=1, le=50)
    stoch_slowing: int = Field(3, ge=1, le=50)
    bb_period: int = Field(20, ge=5, le=200)
    bb_deviation: float = Field(2.0, ge=0.5, le=5.0)
    cci_period: int = Field(14, ge=2, le=200)
    cci_threshold: float = Field(100.0, gt=0, le=500)

    @model_validator(mode="after")
    def _check(self):
        if self.macd_fast >= self.macd_slow:
            raise ValueError("macd_fast must be smaller than macd_slow")
        return self


class ConfirmationConfig(Strict):
    """Mandatory structure/price action first; indicators add confirmation (they are not equal votes)."""
    price_action_mandatory: Literal[True] = True
    enabled_indicators: List[IndicatorName] = ["RSI", "MACD", "BB", "STOCH"]  # CCI optional, off by default
    minimum_confirmations: int = Field(3, ge=0)
    params: IndicatorParams = IndicatorParams()

    @model_validator(mode="after")
    def _check(self):
        if len(set(self.enabled_indicators)) != len(self.enabled_indicators):
            raise ValueError("enabled_indicators contains duplicates")
        if self.minimum_confirmations > len(self.enabled_indicators):
            raise ValueError("minimum_confirmations exceeds the number of enabled indicators")
        return self


class ScalpConfig(Strict):
    """Market-execution scalp: one basket of positions sharing ONE risk budget."""
    target_multiplier: float = Field(3.0, ge=TARGET_MULTIPLIER_MIN, le=TARGET_MULTIPLIER_MAX)  # x basket risk
    scalp_window_minutes: int = Field(15, ge=SCALP_WINDOW_MIN, le=SCALP_WINDOW_MAX)
    timeout_action: TimeoutAction = "CLOSE_ALL"
    max_positions_per_basket: int = Field(1, ge=1, le=BASKET_POSITIONS_MAX)
    risk_split: Literal["equal", "front_loaded"] = "equal"   # how basket risk is shared by positions
    basket_max_loss_percent: Optional[float] = Field(None, gt=0)  # None -> equals basket risk
    max_spread_to_stop_ratio: float = Field(0.15, gt=0, le=1)


class SetupConfig(Strict):
    """M15 setup zones and the M5 trigger window (zone age uses entry.setup_max_bars, M15 bars)."""
    fvg_setup_enabled: bool = True
    sweep_setup_enabled: bool = True
    require_bos_linked_fvg: bool = True
    trigger_window_bars: int = Field(12, ge=1, le=200)       # M5 bars after the zone is touched
    sweep_setup_max_age_bars: int = Field(12, ge=1, le=200)  # M15 bars
    use_premium_discount: bool = True

    @model_validator(mode="after")
    def _check(self):
        if not (self.fvg_setup_enabled or self.sweep_setup_enabled):
            raise ValueError("at least one setup type must be enabled")
        return self


class StructureConfig(Strict):
    swing_left: int = Field(3, ge=1)
    swing_right: int = Field(3, ge=1)
    min_swing_atr: float = Field(0.5, ge=0)
    bos_close_buffer_atr: float = Field(0.05, ge=0)
    disp_body_atr: float = Field(1.2, gt=0)
    disp_body_ratio: float = Field(0.6, gt=0, le=1)
    disp_max_bars: int = Field(3, ge=1)
    max_entry_range_pct: float = Field(50.0, gt=0, le=100)
    htf_mode: Literal["H4_AND_D1", "H4_ONLY"] = "H4_AND_D1"
    execution_tf: Literal["M1", "M5"] = "M5"        # scalp trigger timeframe
    setup_tf: Literal["M15", "M30", "H1"] = "M15"   # setup / pullback / location timeframe

    @model_validator(mode="after")
    def _tf_order(self):
        if TF_MINUTES[self.setup_tf] <= TF_MINUTES[self.execution_tf]:
            raise ValueError("setup_tf must be higher than execution_tf")
        return self


class LiquidityConfig(Strict):
    eq_tolerance_atr: float = Field(0.1, gt=0)
    sweep_min_pierce_atr: float = Field(0.05, ge=0)
    sweep_confirm_bars: int = Field(2, ge=1)
    sweep_required: bool = False  # scored by default; switchable to mandatory
    trap_bars: int = Field(3, ge=1)
    failed_cont_bars: int = Field(3, ge=1)


class FVGConfig(Strict):
    min_atr: float = Field(0.2, ge=0)
    min_points: int = Field(0, ge=0)
    max_age_bars: int = Field(48, ge=1)
    mitigation: Literal["touch", "half", "full"] = "full"


class EntryConfig(Strict):
    entry_mode: Literal["market_confirmed"] = "market_confirmed"  # market order after M5 confirmation
    sl_buffer_atr: float = Field(0.1, ge=0)
    min_sl_atr: float = Field(0.3, gt=0)
    max_sl_atr: float = Field(3.0, gt=0)
    setup_max_bars: int = Field(24, ge=1)
    max_chase_r: float = Field(0.5, ge=0)

    @model_validator(mode="after")
    def _check(self):
        if self.max_sl_atr <= self.min_sl_atr:
            raise ValueError("max_sl_atr must exceed min_sl_atr")
        return self


class AssetClassParams(Strict):
    """Per-asset-class overrides; shared core strategy, different thresholds."""
    max_spread_points: Optional[int] = Field(None, ge=0)
    structure: Optional[StructureConfig] = None
    fvg: Optional[FVGConfig] = None
    volatility: Optional[VolatilityConfig] = None


class BrokerConfig(Strict):
    """Instruments and specifications are always read from the connected terminal."""
    name: str = "Exness"
    symbol_discovery: Literal["terminal"] = "terminal"  # no hardcoded names or suffixes
    allowed_logins: List[int] = Field(default_factory=list, max_length=5)  # MT5 account numbers (never passwords)

    @field_validator("allowed_logins")
    @classmethod
    def _logins(cls, v):
        if any((not isinstance(x, int)) or x <= 0 for x in v) or len(set(v)) != len(v):
            raise ValueError("allowed_logins must be unique positive account numbers")
        return v


class InstrumentConfig(Strict):
    symbol: str = Field(min_length=1)  # canonical/user-facing name
    broker_symbol: Optional[str] = None  # filled by terminal discovery, never assumed
    asset_class: AssetClass = AssetClass.OTHER
    state: InstrumentState = InstrumentState.DISCOVERED
    enabled: bool = True


class InstrumentSelection(Strict):
    mode: Literal["manual", "watchlist", "auto_scan"] = "watchlist"
    watchlist: List[InstrumentConfig] = []
    auto_scan_asset_classes: List[AssetClass] = [AssetClass.FOREX, AssetClass.METALS, AssetClass.CRYPTO]
    exclude_unvalidated: bool = True
    ranking_policy: Literal["score", "lowest_cost", "least_correlated"] = "score"
    per_instrument_max_risk_percent: Optional[float] = Field(None, gt=0)


# --------------------------------------------------------------- prop firm
class PropProfile(Strict):
    name: str
    confirmed: bool = False  # must be True before it can be active
    starting_balance: float = Field(gt=0)
    profit_target_percent: Optional[float] = Field(None, gt=0)
    daily_loss_limit_percent: Optional[float] = Field(None, gt=0)
    max_drawdown_percent: Optional[float] = Field(None, gt=0)
    drawdown_type: Literal["static", "trailing"] = "static"
    drawdown_basis: Literal["balance", "equity"] = "equity"
    daily_loss_reference: Literal["starting_balance", "day_start_balance"] = "starting_balance"  # confirm with the firm
    daily_reset_time: str = Field("00:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    daily_reset_timezone: str = "broker_server"
    count_floating_pl: bool = True
    count_commissions: bool = True
    max_lot_size: Optional[float] = Field(None, gt=0)
    restricted_symbols: List[str] = []
    news_restricted: bool = False
    news_window_min: int = Field(0, ge=0)
    allow_overnight: bool = True
    allow_weekend: bool = True
    min_trading_days: Optional[int] = Field(None, ge=0)
    safety_buffer_percent: float = Field(0.5, ge=0)  # stay this far inside limits
    unresolved_fields: List[str] = []  # parser marks ambiguous/missing values here


class SystemSafety(Strict):
    stale_config_seconds: int = Field(300, ge=30)
    heartbeat_seconds: int = Field(10, ge=1)
    kill_switch: bool = False


# -------------------------------------------------------------- root config
class SystemConfig(Strict):
    schema_version: int = SCHEMA_VERSION
    config_version: int = Field(1, ge=1)
    mode: Mode = Mode.SIGNAL_ONLY
    live_confirmed: bool = False
    magic_number: int = Field(240001, ge=1)
    risk: RiskConfig = RiskConfig()
    scalp: ScalpConfig = ScalpConfig()
    confirmation: ConfirmationConfig = ConfirmationConfig()
    setup: SetupConfig = SetupConfig()
    break_even: BreakEvenConfig = BreakEvenConfig()
    daily: DailyLimits = DailyLimits()
    schedule: ScheduleConfig = ScheduleConfig()
    news: NewsConfig = NewsConfig()
    volatility: VolatilityConfig = VolatilityConfig()
    portfolio: PortfolioConfig = PortfolioConfig()
    structure: StructureConfig = StructureConfig()
    liquidity: LiquidityConfig = LiquidityConfig()
    fvg: FVGConfig = FVGConfig()
    entry: EntryConfig = EntryConfig()
    asset_class_params: Dict[AssetClass, AssetClassParams] = {}
    instruments: InstrumentSelection = InstrumentSelection()
    prop_profiles: Dict[str, PropProfile] = {}
    active_prop_profile: Optional[str] = None
    safety: SystemSafety = SystemSafety()
    broker: BrokerConfig = BrokerConfig()

    @model_validator(mode="after")
    def _cross_checks(self):
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version {self.schema_version}")
        if self.risk.max_open_risk_percent < self.risk.risk_percent:
            raise ValueError("max_open_risk_percent must be >= risk_percent")
        if self.daily.lockout_after_losses > self.daily.max_trades_per_day:
            # lockout can never trigger; allowed but suspicious -> reject to avoid false safety
            raise ValueError("lockout_after_losses exceeds max_trades_per_day")
        if self.scalp.max_positions_per_basket > self.risk.max_simultaneous_positions:
            raise ValueError("scalp.max_positions_per_basket exceeds risk.max_simultaneous_positions")
        bml = self.scalp.basket_max_loss_percent
        if bml is not None and bml > self.risk.risk_percent:
            raise ValueError("basket_max_loss_percent cannot exceed the basket risk (risk.risk_percent)")
        if self.active_prop_profile is not None:
            p = self.prop_profiles.get(self.active_prop_profile)
            if p is None:
                raise ValueError("active_prop_profile not found")
            if not p.confirmed:
                raise ValueError("active prop profile is not confirmed")
            if p.unresolved_fields:
                raise ValueError(f"prop profile has unresolved fields: {p.unresolved_fields}")
        if self.mode == Mode.LIVE:
            if not self.live_confirmed:
                raise ValueError("LIVE mode requires live_confirmed")
            bad = [i.symbol for i in self.instruments.watchlist
                   if i.enabled and i.state != InstrumentState.LIVE_VALIDATED]
            if bad:
                raise ValueError(f"unvalidated instruments in LIVE mode: {bad}")
        return self

    # ----------------------------------------------------------- utilities
    def ea_payload(self) -> dict:
        """Config as sent to the EA: full config + derived, backend-resolved values."""
        from .trading_day import parse_hhmm
        data = self.model_dump(mode="json")
        data["derived"] = {
            "reset_utc_offset_seconds": resolve_fixed_offset(self.daily.reset_timezone),
            "reset_minutes": parse_hhmm(self.daily.reset_time),
            "schedule_utc_offset_seconds": resolve_fixed_offset(self.schedule.timezone),
            "scalp_window_seconds": self.scalp.scalp_window_minutes * 60,
        }
        data["config_hash"] = self.config_hash()
        return data

    def canonical_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))

    def config_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()


def risk_increase_requires_confirmation(old: SystemConfig, new: SystemConfig) -> bool:
    return new.risk.risk_percent > old.risk.risk_percent


def effective_loss_limits(cfg: SystemConfig) -> Dict[str, Optional[float]]:
    """Strictest applicable daily-loss limit (percent), after the prop safety buffer."""
    limits: List[float] = []
    if cfg.daily.daily_loss_limit_percent is not None:
        limits.append(cfg.daily.daily_loss_limit_percent)
    if cfg.active_prop_profile:
        p = cfg.prop_profiles[cfg.active_prop_profile]
        if p.daily_loss_limit_percent is not None:
            limits.append(max(p.daily_loss_limit_percent - p.safety_buffer_percent, 0.0))
    return {"daily_loss_percent": min(limits) if limits else None}


def migrate_config(data: dict) -> dict:
    """Upgrade a stored v2 config dict (pending-order design) to schema v3 (market scalper)."""
    import copy
    d = copy.deepcopy(data)
    version = d.get("schema_version", 1)
    if version == 3:
        return d
    if version != 2:
        raise ValueError(f"cannot migrate schema_version {version}")
    d.pop("derived", None)
    d.pop("config_hash", None)
    d.pop("rr", None)  # fixed per-trade RR replaced by scalp.target_multiplier (basket target)
    d.setdefault("entry", {})["entry_mode"] = "market_confirmed"
    structure = d.setdefault("structure", {})
    if structure.get("execution_tf") == "M15":
        structure["execution_tf"] = "M5"
    d["schema_version"] = 3
    return d
