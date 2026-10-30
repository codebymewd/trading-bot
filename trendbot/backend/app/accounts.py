"""Multi-account registry (isolated per-account state, no hard-coded account limit).

Every trading account has its own:
  name / MT5 login / broker / account type / initial balance / prop-firm profile /
  risk settings / daily counters / open positions / trade history / EA connection /
  trading status / configuration / statistics / audit history.

Design notes
- `user_id` is reserved for the future multi-user structure (User -> Accounts -> EA -> MT5);
  it is nullable and never used as a control identity today.
- The DEFAULT account mirrors the legacy single-account behaviour exactly: when only the
  default account exists and nothing was customised, its effective config is the global one.
- EA identity verification: an EA heartbeat carrying login L may only serve accounts whose
  mt5_login == L (or mt5_login is unset, until verified). We never trust manually entered
  information alone: `ea_verified_login` is recorded from the EA's own ACCOUNT_LOGIN report.
- Statuses: ACTIVE | PAUSED | BLOCKED | OFFLINE | ERROR | ARCHIVED.
  New trades require ACTIVE + a fresh, verified EA connection + all safety gates.
"""
from __future__ import annotations

import copy
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .config_schema import PropProfile, SystemConfig
from .db import Db

STATUSES = ("ACTIVE", "PAUSED", "BLOCKED", "OFFLINE", "ERROR", "ARCHIVED")
TRADEABLE = {"ACTIVE"}


@dataclass
class Account:
    id: str
    name: str
    user_id: Optional[str] = None
    mt5_login: Optional[int] = None
    broker: str = ""
    account_type: str = "demo"           # demo | live (live stays locked by mode policy)
    initial_balance: float = 0.0
    currency: str = "USD"
    prop_profile: Optional[str] = None   # name of the profile inside the account config
    active: bool = True                  # activation switch (independent of status)
    status: str = "PAUSED"               # see STATUSES
    archived: bool = False
    config_overrides: Dict[str, Any] = field(default_factory=dict)
    created_at: int = 0
    note: str = ""

    def to_dict(self) -> dict:
        return copy.deepcopy(self.__dict__)


def _dig(data: dict, path, value) -> None:
    d = data
    for p in path[:-1]:
        if not isinstance(d.get(p), dict):
            d[p] = {}
        d = d[p]
    d[path[-1]] = value


class AccountStore:
    """Accounts persisted in the shared SQLite db (survives backend/computer restarts)."""

    def __init__(self, db: Db, clock: Callable[[], float] = time.time):
        self.db, self.clock = db, clock
        if self.db.kv_get("accounts_missing_sentinel") is None and not self.db.kv_get("accounts"):
            # first run: create the default account so existing single-account setups keep working
            default = Account(id="default", name="Default", created_at=int(clock()))
            self.db.kv_set("accounts", {default.id: default.to_dict()})
        else:
            stored = self.db.kv_get("accounts") or {}
            if "default" not in stored:      # upgraded from an older build
                stored["default"] = Account(id="default", name="Default", created_at=int(clock())).to_dict()
                self.db.kv_set("accounts", stored)

    # ---- persistence helpers
    def _persist(self, items: Dict[str, dict]) -> None:
        self.db.kv_set("accounts", items)

    # ---- CRUD
    def list(self, include_archived: bool = False) -> List[Account]:
        raw = self.db.kv_get("accounts") or {}
        out = [Account(**{k: v for k, v in d.items()}) for d in raw.values()]
        out.sort(key=lambda a: (a.archived, a.created_at, a.id))
        return out if include_archived else [a for a in out if not a.archived]

    def get(self, account_id: str) -> Optional[Account]:
        raw = self.db.kv_get("accounts") or {}
        d = raw.get(account_id)
        return Account(**d) if d else None

    def save(self, acc: Account, actor: str = "dashboard") -> None:
        raw = self.db.kv_get("accounts") or {}
        raw[acc.id] = acc.to_dict()
        self._persist(raw)
        self.db.audit(int(self.clock()), actor, "ACCOUNT_SAVED", {"id": acc.id, "name": acc.name})

    def create(self, *, name: str, user_id: Optional[str] = None, mt5_login: Optional[int] = None,
               broker: str = "", account_type: str = "demo", initial_balance: float = 0.0,
               currency: str = "USD", prop_profile: Optional[str] = None,
               overrides: Optional[Dict[str, Any]] = None, actor: str = "dashboard") -> Account:
        name = (name or "").strip()
        if not name:
            raise ValueError("account name is required")
        if any(a.name.lower() == name.lower() for a in self.list(include_archived=True)):
            raise ValueError("an account with that name already exists")
        if mt5_login is not None and (not isinstance(mt5_login, int) or mt5_login <= 0):
            raise ValueError("mt5_login must be a positive account number (never a password)")
        sid = uuid4hex()
        acc = Account(id=sid, name=name, user_id=user_id, mt5_login=int(mt5_login) if mt5_login else None,
                      broker=broker, account_type=account_type if account_type in ("demo", "live") else "demo",
                      initial_balance=float(initial_balance or 0.0), currency=currency,
                      prop_profile=prop_profile, active=False, status="PAUSED",
                      config_overrides=overrides or {}, created_at=int(self.clock()))
        self.save(acc, actor)
        return acc

    def update(self, account_id: str, actor: str = "dashboard", **fields) -> Account:
        acc = self.get(account_id)
        if acc is None:
            raise KeyError(f"unknown account {account_id}")
        allowed = {"name", "mt5_login", "broker", "account_type", "initial_balance", "currency",
                   "prop_profile", "config_overrides", "note", "user_id"}
        changed = {}
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k == "mt5_login" and v is not None and (not isinstance(v, int) or v <= 0):
                raise ValueError("mt5_login must be a positive account number")
            if k == "name" and not str(v).strip():
                raise ValueError("account name is required")
            setattr(acc, k, v)
            changed[k] = v
        self.save(acc, actor)
        if changed:
            self.db.audit(int(self.clock()), actor, "ACCOUNT_UPDATED", {"id": account_id, "fields": sorted(changed)})
        return acc

    def set_status(self, account_id: str, status: str, reason: str = "", actor: str = "system") -> Account:
        if status not in STATUSES:
            raise ValueError(f"invalid status {status}")
        acc = self.get(account_id)
        if acc is None:
            raise KeyError(f"unknown account {account_id}")
        acc.status = status
        self.save(acc, actor)
        self.db.audit(int(self.clock()), actor, "ACCOUNT_STATUS", {"id": account_id, "status": status, "reason": reason})
        return acc

    def activate(self, account_id: str, actor: str = "dashboard") -> Account:
        """Activation is conditional: the caller (service layer) must pass the readiness check."""
        acc = self.get(account_id)
        if acc is None:
            raise KeyError(f"unknown account {account_id}")
        if acc.archived:
            raise ValueError("archived accounts cannot be activated")
        acc.active = True
        acc.status = "ACTIVE"
        self.save(acc, actor)
        self.db.audit(int(self.clock()), actor, "ACCOUNT_ACTIVATED", {"id": account_id})
        return acc

    def pause(self, account_id: str, actor: str = "dashboard") -> Account:
        """Pause: no NEW trades; existing positions keep their SL/TP and basket management continues."""
        acc = self.set_status(account_id, "PAUSED", "paused by user", actor)
        acc.active = False
        self.save(acc, actor)
        return acc

    def archive(self, account_id: str, actor: str = "dashboard") -> None:
        acc = self.get(account_id)
        if acc is None:
            raise KeyError(f"unknown account {account_id}")
        if acc.id == "default":
            raise ValueError("the default account cannot be archived")
        acc.archived, acc.active, acc.status = True, False, "ARCHIVED"
        self.save(acc, actor)
        self.db.audit(int(self.clock()), actor, "ACCOUNT_ARCHIVED", {"id": account_id})

    # ---- EA identity verification
    def verify_ea(self, account_id: str, login: int) -> Optional[Account]:
        """Record the EA-reported login and check it matches the configured one. Returns the account on match."""
        acc = self.get(account_id)
        if acc is None:
            return None
        if login is None or int(login) <= 0:
            return None
        if acc.mt5_login is not None and int(acc.mt5_login) != int(login):
            self.db.audit(int(self.clock()), "ea", "EA_IDENTITY_MISMATCH",
                          {"id": account_id, "configured": acc.mt5_login, "reported": login})
            return None
        st = self.db.kv_get(f"acct_state:{account_id}") or {}
        if st.get("ea_verified_login") != int(login):
            st["ea_verified_login"] = int(login)
            if st.get("ea_login") is None:
                st["ea_login"] = int(login)
            self.db.kv_set(f"acct_state:{account_id}", st)
            self.db.audit(int(self.clock()), "ea", "EA_IDENTITY_VERIFIED", {"id": account_id, "login": login})
        return acc

    def ea_state(self, account_id: str) -> dict:
        return self.db.kv_get(f"acct_state:{account_id}") or {}

    # ---- effective configuration -------------------------------------------------
    def effective_config(self, acc: Account, base: SystemConfig) -> SystemConfig:
        """base config + per-account overrides (validated by the same schema)."""
        ov = acc.config_overrides or {}
        data = base.model_dump(mode="json")
        for section, values in (ov.get("sections") or {}).items():
            if isinstance(values, dict):
                for k, v in values.items():
                    data.setdefault(section, {})[k] = v
        for path, value in (ov.get("paths") or []):
            _dig(data, tuple(path), value)
        if acc.prop_profile:
            prof = ov.get("prop_profiles", {}).get(acc.prop_profile)
            if prof is not None:
                data["prop_profiles"][acc.prop_profile] = prof
            data["active_prop_profile"] = acc.prop_profile
        elif "prop_profiles" in ov:
            data["prop_profiles"] = ov["prop_profiles"]
        data["config_version"] = int(ov.get("config_version", base.config_version))
        return SystemConfig(**data)

    def bump_version(self, acc: Account, base: SystemConfig) -> int:
        ov = acc.config_overrides or {}
        v = int(ov.get("config_version", base.config_version)) + 1
        ov["config_version"] = v
        acc.config_overrides = ov
        return v


def uuid4hex() -> str:
    import uuid
    return uuid.uuid4().hex[:12]


def prop_from_account(acc: Account, cfg: SystemConfig) -> Optional[PropProfile]:
    """The account's active prop profile resolved against its effective config."""
    if cfg.active_prop_profile:
        return cfg.prop_profiles.get(cfg.active_prop_profile)
    return None
