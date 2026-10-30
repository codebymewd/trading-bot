"""Editable settings exposed to Telegram. Every change is built into a FULL candidate SystemConfig and validated by
the schema (and the EA validates again with the shared contract), so Telegram input is never trusted."""
from __future__ import annotations

import copy
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from pydantic import ValidationError

from .config_schema import SystemConfig, effective_loss_limits
from .db import Db


@dataclass(frozen=True)
class Setting:
    key: str
    label: str
    path: Tuple[str, ...]
    kind: str                       # float | int | bool | enum | toggle
    choices: Tuple = ()
    presets: Tuple = ()
    unit: str = ""
    always_confirm: bool = False    # high impact: always needs the confirm button
    confirm_on_increase: bool = False


SETTINGS: Dict[str, Setting] = {s.key: s for s in [
    Setting("risk", "Risk per basket", ("risk", "risk_percent"), "float", presets=(0.5, 1.0, 2.0, 3.0, 5.0), unit="%", confirm_on_increase=True),
    Setting("open_risk", "Max open risk", ("risk", "max_open_risk_percent"), "float", presets=(1.0, 2.0, 5.0), unit="%", confirm_on_increase=True),
    Setting("target", "Target multiplier", ("scalp", "target_multiplier"), "float", presets=(1.0, 2.0, 3.0, 4.0, 5.0), unit="x", confirm_on_increase=True),
    Setting("window", "Scalp window", ("scalp", "scalp_window_minutes"), "int", presets=(5, 15, 30), unit="min"),
    Setting("timeout", "Timeout action", ("scalp", "timeout_action"), "enum",
            choices=("CLOSE_ALL", "MOVE_TO_BREAKEVEN", "KEEP_RUNNING", "CLOSE_IF_LOSS")),
    Setting("basket_positions", "Positions per basket", ("scalp", "max_positions_per_basket"), "int", presets=(1, 2, 3), confirm_on_increase=True),
    Setting("max_positions", "Max simultaneous positions", ("risk", "max_simultaneous_positions"), "int", presets=(1, 2, 3, 5), confirm_on_increase=True),
    Setting("split", "Risk split", ("scalp", "risk_split"), "enum", choices=("equal", "front_loaded")),
    Setting("be", "Break-even method", ("break_even", "method"), "enum", choices=("off", "A", "B", "C")),
    Setting("be_trigger", "Break-even trigger", ("break_even", "trigger_r"), "float", presets=(0.5, 1.0, 1.5), unit="R"),
    Setting("be_offset", "Break-even offset", ("break_even", "be_offset_points"), "int", presets=(0, 10, 20), unit="points"),
    Setting("partial", "Partial close (method B)", ("break_even", "partial_close_percent"), "float", presets=(0.0, 25.0, 50.0), unit="%"),
    Setting("min_conf", "Minimum indicator confirmations", ("confirmation", "minimum_confirmations"), "int", presets=(0, 1, 2, 3, 4)),
    Setting("ind", "Indicators on/off", ("confirmation", "enabled_indicators"), "toggle", choices=("RSI", "MACD", "BB", "STOCH", "CCI")),
    Setting("vol", "Volatility states allowed", ("volatility", "allowed_states"), "toggle", choices=("normal", "elevated")),
    Setting("news_high", "Trade during high-impact news", ("news", "high", "allow_entries"), "bool", always_confirm=True),
    Setting("news_medium", "Trade during medium-impact news", ("news", "medium", "allow_entries"), "bool"),
    Setting("news_low", "Trade during low-impact news", ("news", "low", "allow_entries"), "bool"),
    Setting("news_missing", "If news data is missing", ("news", "on_data_unavailable"), "enum", choices=("block", "allow"), always_confirm=True),
    Setting("trades_day", "Baskets per day", ("daily", "max_trades_per_day"), "int", presets=(1, 2, 3), confirm_on_increase=True),
    Setting("daily_loss", "Daily loss limit", ("daily", "daily_loss_limit_percent"), "float", presets=(2.0, 3.0, 5.0), unit="%", confirm_on_increase=True),
    # LIVE is intentionally NOT offered here: the executor hard-blocks it (LIVE_TRADING_AVAILABLE=False)
    # until a completed DEMO forward test. See docs/STATUS.md for the unlock procedure.
    Setting("mode", "Trading mode", ("mode",), "enum", choices=("SIGNAL_ONLY", "DEMO"), always_confirm=True),
    Setting("gate", "Trend gate", ("structure", "htf_mode"), "enum", choices=("H4_AND_D1", "H4_ONLY"), always_confirm=True),
    Setting("profile", "Active prop-firm profile", ("active_prop_profile",), "enum", always_confirm=True),
    Setting("kill", "Kill switch (block new orders)", ("safety", "kill_switch"), "bool"),
]}


def get_path(d: dict, path: Tuple[str, ...]) -> Any:
    for p in path:
        d = d[p]
    return d


def set_path(d: dict, path: Tuple[str, ...], value: Any) -> None:
    for p in path[:-1]:
        d = d[p]
    d[path[-1]] = value


def parse_value(s: Setting, text: str, current: Any) -> Any:
    t = text.strip()
    if s.kind == "float":
        if t.lower() in ("off", "none") and s.key == "daily_loss":
            return None
        return float(t.rstrip("%x"))
    if s.kind == "int":
        return int(float(t))
    if s.kind == "bool":
        if t.lower() in ("on", "true", "yes", "1"):
            return True
        if t.lower() in ("off", "false", "no", "0"):
            return False
        raise ValueError("expected on/off")
    if s.kind == "enum":
        v = t.upper() if t.upper() in [c.upper() for c in s.choices] and s.key != "be" else t
        if s.key in ("be", "news_missing", "split"):
            match = [c for c in s.choices if c.lower() == t.lower()]
        else:
            match = [c for c in s.choices if c.upper() == t.upper()]
        if s.key == "profile":
            return t
        if not match:
            raise ValueError("choose one of: " + ", ".join(s.choices))
        return match[0]
    if s.kind == "toggle":
        match = [c for c in s.choices if c.lower() == t.lower()]
        if not match:
            raise ValueError("choose one of: " + ", ".join(s.choices))
        lst = list(current)
        item = match[0]
        if item in lst:
            lst.remove(item)
        else:
            lst.append(item)
        return [c for c in s.choices if c in lst]       # keep canonical order
    raise ValueError("unsupported setting")


def prop_conflict(cfg: SystemConfig) -> str:
    """'' | 'extra' (needs a second confirmation) | 'block' (mandatory restriction, cannot be confirmed away)."""
    if not cfg.active_prop_profile:
        return ""
    limit = effective_loss_limits(cfg)["daily_loss_percent"]
    if limit is None:
        return ""
    if cfg.risk.risk_percent >= limit:
        return "block"
    if cfg.risk.risk_percent * 2 > limit:
        return "extra"
    return ""


@dataclass
class Proposal:
    id: str
    key: str
    label: str
    old: Any
    new: Any
    unit: str
    needs_confirm: bool
    extra_confirm: bool
    candidate: SystemConfig
    created: int
    stage: int = 1
    note: str = ""


@dataclass
class ChangeResult:
    ok: bool
    message: str
    proposal: Optional[Proposal] = None
    applied_now: bool = False
    version: int = 0


class ConfigManager:
    """Current/pending/applied configuration with an audit trail.
    A change is 'applied' only after the EA acknowledges it (on_ack) - never on Telegram's say-so."""

    def __init__(self, db: Db, initial: Optional[SystemConfig] = None, clock: Callable[[], float] = time.time):
        self.db, self.clock = db, clock
        row = db.latest_config()
        if row:
            self.current = SystemConfig(**_strip(__import__("json").loads(row["json"])))
        else:
            self.current = initial or SystemConfig()
            db.save_config(self.current.config_version, self.current.canonical_json(), self.current.config_hash(),
                           int(clock()), "system", "applied")
        self.proposals: Dict[str, Proposal] = {}

    @property
    def applied_version(self) -> int:
        return self.db.last_applied_version()

    def view(self) -> dict:
        return self.current.model_dump(mode="json")

    def propose(self, actor: str, key: str, text: str) -> ChangeResult:
        s = SETTINGS.get(key)
        if s is None:
            return ChangeResult(False, f"Unknown setting '{key}'.")
        data = self.view()
        old = get_path(data, s.path)
        try:
            if key == "profile" and text.strip().lower() not in {p.lower() for p in self.current.prop_profiles}:
                raise ValueError("saved profiles: " + (", ".join(self.current.prop_profiles) or "none"))
            new = parse_value(s, text, old)
            if key == "profile":
                new = next(p for p in self.current.prop_profiles if p.lower() == text.strip().lower())
            set_path(data, s.path, new)
            side = ""
            if key == "risk" and isinstance(new, (int, float)) and new > data["risk"]["max_open_risk_percent"]:
                data["risk"]["max_open_risk_percent"] = new      # one basket at a time: the open-risk cap must cover it
                side = f" Max open risk is raised to {new}% to match."
            data["config_version"] = self.current.config_version + 1
            candidate = SystemConfig(**data)
        except (ValueError, ValidationError) as e:
            msg = e.errors()[0]["msg"] if isinstance(e, ValidationError) else str(e)
            self.db.audit(int(self.clock()), actor, "REJECTED_INPUT", {"key": key, "value": text, "error": msg})
            return ChangeResult(False, f"Not accepted: {msg}")
        conflict = prop_conflict(candidate)
        if conflict == "block":
            self.db.audit(int(self.clock()), actor, "BLOCKED_BY_PROP_RULES", {"key": key, "value": str(new)})
            return ChangeResult(False, "Blocked: this risk would conflict with the active prop-firm limits. "
                                       "Lower it, or change the profile first. Confirmation cannot override this.")
        increase = isinstance(old, (int, float)) and isinstance(new, (int, float)) and not isinstance(new, bool) and new > old
        needs = s.always_confirm or (s.confirm_on_increase and increase) or (key == "kill" and new is False)
        extra = conflict == "extra" and key == "risk" and increase
        p = Proposal(uuid.uuid4().hex[:10], key, s.label, old, new, s.unit, needs or extra, extra, candidate, int(self.clock()))
        if not p.needs_confirm:
            r = self._commit(actor, p)
            r.message += side
            return r
        self.proposals[p.id] = p
        note = ("Applies to future baskets only; open baskets keep their current risk." if key in ("risk", "open_risk", "target") else "") + side
        p.note = note.strip()
        return ChangeResult(True, f"{s.label}: {_fmt(old, s.unit)} -> {_fmt(new, s.unit)}. {note}".strip(), p)

    def propose_custom(self, actor: str, key: str, label: str, mutate: Callable[[dict], None],
                       always_confirm: bool = True) -> ChangeResult:
        """For changes that are not a single registry setting (watchlist edits, ...). Same validation and audit."""
        data = self.view()
        try:
            mutate(data)
            data["config_version"] = self.current.config_version + 1
            candidate = SystemConfig(**data)
        except (ValueError, ValidationError, KeyError) as e:
            msg = e.errors()[0]["msg"] if isinstance(e, ValidationError) else str(e)
            self.db.audit(int(self.clock()), actor, "REJECTED_INPUT", {"key": key, "error": msg})
            return ChangeResult(False, f"Not accepted: {msg}")
        p = Proposal(uuid.uuid4().hex[:10], key, label, "", "", "", always_confirm, False, candidate, int(self.clock()))
        if not always_confirm:
            return self._commit(actor, p)
        self.proposals[p.id] = p
        return ChangeResult(True, label, p)

    def confirm(self, actor: str, proposal_id: str) -> ChangeResult:
        p = self.proposals.get(proposal_id)
        if p is None:
            return ChangeResult(False, "That request expired or was already handled.")
        if self.clock() - p.created > 300:
            del self.proposals[proposal_id]
            return ChangeResult(False, "That request expired (5 minutes). Please ask again.")
        if p.extra_confirm and p.stage == 1:
            p.stage = 2
            return ChangeResult(True, "This risk is large compared with your active prop-firm limits. Confirm once more to proceed.", p)
        del self.proposals[proposal_id]
        if self.current.config_version + 1 != p.candidate.config_version:      # config changed since the proposal
            return ChangeResult(False, "The configuration changed in the meantime. Please ask again.")
        return self._commit(actor, p)

    def cancel(self, actor: str, proposal_id: str) -> ChangeResult:
        self.proposals.pop(proposal_id, None)
        self.db.audit(int(self.clock()), actor, "CANCELLED", {"id": proposal_id})
        return ChangeResult(True, "Cancelled. Nothing was changed.")

    def _commit(self, actor: str, p: Proposal) -> ChangeResult:
        self.current = p.candidate
        self.db.save_config(self.current.config_version, self.current.canonical_json(), self.current.config_hash(),
                            int(self.clock()), actor, "pending")
        self.db.audit(int(self.clock()), actor, "CONFIG_CHANGE_REQUESTED",
                      {"key": p.key, "old": p.old, "new": p.new, "version": self.current.config_version})
        return ChangeResult(True, f"Saved as version {self.current.config_version}. Waiting for the EA to confirm it is applied.",
                            p, True, self.current.config_version)

    def on_ack(self, version: int, cfg_hash: str, ok: bool, error: str = "") -> str:
        row = self.db.latest_config() if version == self.current.config_version else None
        if version != self.current.config_version:
            return f"Ignored acknowledgement for old version {version}."
        if ok and cfg_hash == self.current.config_hash():
            self.db.set_config_status(version, "applied")
            self.db.audit(int(self.clock()), "ea", "CONFIG_APPLIED", {"version": version})
            return f"Configuration version {version} is applied by the EA."
        self.db.set_config_status(version, "failed")
        self.db.audit(int(self.clock()), "ea", "CONFIG_FAILED", {"version": version, "error": error or "hash mismatch"})
        return f"The EA could NOT apply configuration version {version}: {error or 'hash mismatch'}. It keeps its previous settings."


def _strip(d: dict) -> dict:
    d = dict(d)
    d.pop("derived", None)
    d.pop("config_hash", None)
    return d


def _fmt(v: Any, unit: str) -> str:
    if v is None:
        return "off"
    if isinstance(v, bool):
        return "on" if v else "off"
    if isinstance(v, list):
        return ", ".join(map(str, v)) or "none"
    return f"{v}{unit}" if unit in ("%", "x") else (f"{v} {unit}".strip())
