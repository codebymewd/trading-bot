"""Dashboard API: read-only overview + controls, served by the SAME backend as the EA endpoints.

Architecture (Telegram is NOT in this path):
    Dashboard -> Backend (/api/*) -> account configuration -> EA (/ea/*) -> MT5 -> Broker

Auth: TB_DASHBOARD_TOKEN environment variable. Every /api request must carry
`Authorization: Bearer <token>` (or X-Dashboard-Token). If no token is configured the API is
disabled (403) rather than open - fail safe. Secrets are never returned to the dashboard.

The dashboard reads per-account state that the service persists:
  acct_state:<id>   written by the heartbeat handler (EA online, verified login, applied version)
  acct_store:<id>   BasketStore JSON file path for that account (baskets/counters/used_setups)
Settings changes go through ConfigManager (propose -> new version/hash -> EA ack = "Applied").
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from .accounts import AccountStore, prop_from_account
from .drawdown import drawdown_state
from .executor import BasketStore, open_risk_total


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        self.status, self.message = status, message


def _store_for(service, account_id: str) -> BasketStore:
    """Per-account durable BasketStore (restart-safe daily counters & baskets)."""
    st = service.db.kv_get(f"acct_store:{account_id}")
    if st:
        return BasketStore.load(st)
    # legacy single global store kept under 'default' id for backwards compatibility
    glob = getattr(service, "store_path", None)
    if glob and account_id == "default":
        return BasketStore.load(glob)
    return BasketStore()          # empty, in-memory (no trading happens against it anyway)


def build_overview(service, acc) -> Dict[str, Any]:
    cfg = service.accounts.effective_config(acc, service.cfg.current)
    store = _store_for(service, acc.id)
    ea = service.accounts.ea_state(acc.id)
    now = int(service.clock())
    hb_age = now - int(ea.get("ts", 0)) if ea.get("ts") else None
    ea_online = hb_age is not None and hb_age <= service.s.heartbeat_timeout

    profile = prop_from_account(acc, cfg)
    bal = float(ea.get("balance", acc.initial_balance) or acc.initial_balance)
    eq = float(ea.get("equity", bal) or bal)
    day_start = float(ea.get("day_start_balance", ea.get("balance", bal)) or bal)
    hwm = float(ea.get("high_water", max(bal, eq, acc.initial_balance)) or max(bal, eq))
    dd = drawdown_state(profile, balance=bal, equity=eq, day_start_balance=day_start,
                        high_water=hwm, open_risk_money=open_risk_total(store.open_baskets()))

    positions = []
    for b in store.open_baskets():
        for p in b.positions:
            if p.status != "OPEN":
                continue
            positions.append({
                "ticket": p.ticket, "symbol": b.spec.symbol, "direction": "BUY" if b.direction == 1 else "SELL",
                "lots": p.lots, "entry": p.entry_price, "sl": p.sl, "tp": p.tp,
                "basket": b.id, "opened_at": b.opened_at,
                "risk_money": round(p.risk_money, 2), "status": p.status,
            })

    wins = losses = 0
    closed = [b for b in store.baskets.values() if b.status == "CLOSED"]
    for b in closed:
        if b.final_pnl > 0:
            wins += 1
        elif b.final_pnl < 0:
            losses += 1
    total = wins + losses
    pnl_today = store.counters.realized_net

    def level(used_pct: Optional[float]) -> str:
        if used_pct is None:
            return "SAFE"
        if used_pct >= 80:
            return "BLOCKED"
        if used_pct >= 60:
            return "WARNING"
        return "SAFE"

    overall_pct = (dd.current_drawdown_money / dd.max_drawdown_money * 100.0
                   if dd.max_drawdown_money and dd.max_drawdown_money != float("inf") else None)
    daily_pct = (dd.daily_loss_used_money / dd.daily_loss_limit_money * 100.0
                 if dd.daily_loss_limit_money else None)

    trading_on = (acc.status == "ACTIVE" and ea_online and not store.counters.locked_out
                  and cfg.mode.value in ("demo", "signal_only"))
    reasons = []
    if acc.status != "ACTIVE":
        reasons.append(f"account_status_{acc.status}")
    if not ea_online:
        reasons.append("ea_offline")
    if store.counters.locked_out:
        reasons.append("lockout_active")
    if cfg.safety.kill_switch:
        reasons.append("kill_switch")
    if dd.blocked_reason:
        reasons.append(dd.blocked_reason)

    return {
        "account": {"id": acc.id, "name": acc.name, "broker": acc.broker, "mt5_login": acc.mt5_login,
                    "status": acc.status, "mode": cfg.mode.value, "currency": acc.currency},
        "backend": {"online": True, "server_time": now},
        "ea": {"online": ea_online, "heartbeat_age_seconds": hb_age,
               "verified_login": ea.get("ea_verified_login"), "configured_login": acc.mt5_login,
               "identity_verified": ea.get("ea_verified_login") is not None
               and (acc.mt5_login is None or ea.get("ea_verified_login") == acc.mt5_login),
               "applied_version": ea.get("applied_version"), "config_version": cfg.config_version,
               "config_applied": ea.get("applied_version") == cfg.config_version
               and service.cfg.applied_version >= cfg.config_version},
        "trading": {"on": trading_on, "blocked_reasons": reasons,
                    "state": "ON" if trading_on else "BLOCKED"},
        "money": {"balance": bal, "equity": eq, "floating_pl": round(eq - bal, 2),
                  "today_pl": round(pnl_today, 2)},
        "prop": {"initial_balance": dd.initial_balance, "max_drawdown_money":
                 None if dd.max_drawdown_money == float("inf") else round(dd.max_drawdown_money, 2),
                 "current_drawdown_money": round(dd.current_drawdown_money, 2),
                 "remaining_drawdown_money": None if dd.remaining_drawdown_money == float("inf")
                 else round(dd.remaining_drawdown_money, 2),
                 "daily_limit_money": None if dd.daily_loss_limit_money is None else round(dd.daily_loss_limit_money, 2),
                 "daily_used_money": round(dd.daily_loss_used_money, 2),
                 "daily_remaining_money": None if dd.daily_loss_remaining_money is None
                 else round(dd.daily_loss_remaining_money, 2),
                 "open_risk_money": round(dd.open_risk_money, 2),
                 "next_trade_risk_money": None if dd.next_trade_risk_money == float("inf")
                 else round(dd.next_trade_risk_money, 2),
                 "overall_used_pct": None if overall_pct is None else round(overall_pct, 1),
                 "daily_used_pct": None if daily_pct is None else round(daily_pct, 1),
                 "overall_status": level(overall_pct), "daily_status": level(daily_pct)},
        "stats": {"risk_percent": cfg.risk.risk_percent, "target_multiplier": cfg.scalp.target_multiplier,
                  "trades_today": store.counters.trades_opened, "max_trades_per_day": cfg.daily.max_trades_per_day,
                  "consecutive_losses": store.counters.consecutive_losses,
                  "win_rate": (round(wins / total * 100.0, 1) if total else None),
                  "locked_out": store.counters.locked_out},
        "positions": positions,
    }


def register_api(app: FastAPI, service, token_provider) -> None:
    """Adds /api/* routes and serves the dashboard UI from dashboard/."""
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
    dash_dir = os.path.normpath(os.path.join(root, "dashboard"))

    def auth(request: Request) -> Optional[JSONResponse]:
        tok = token_provider()
        if not tok:
            return JSONResponse({"error": "dashboard_api_disabled"}, status_code=403)
        hdr = request.headers.get("authorization", "")
        given = hdr[7:] if hdr.lower().startswith("bearer ") else request.headers.get("x-dashboard-token", "")
        if given != tok:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return None

    def get_acc(account_id: Optional[str]):
        accounts: AccountStore = service.accounts
        if not account_id:
            account_id = service.db.kv_get("selected_account") or accounts.list()[0].id
        acc = accounts.get(account_id)
        if acc is None:
            raise ApiError(404, "unknown account")
        return acc

    @app.get("/api/overview")
    def overview(request: Request, account: Optional[str] = None):
        err = auth(request)
        if err:
            return err
        try:
            return build_overview(service, get_acc(account))
        except ApiError as e:
            return JSONResponse({"error": e.message}, status_code=e.status)

    @app.get("/api/accounts")
    def accounts_list(request: Request):
        err = auth(request)
        if err:
            return err
        out = []
        for a in service.accounts.list():
            st = service.accounts.ea_state(a.id)
            out.append({**a.to_dict(), "ea_online": bool(st.get("online")),
                        "ea_verified_login": st.get("ea_verified_login")})
        return {"accounts": out, "selected": service.db.kv_get("selected_account")}

    @app.post("/api/accounts")
    async def accounts_create(request: Request):
        err = auth(request)
        if err:
            return err
        body = await request.json()
        try:
            acc = service.accounts.create(
                name=body.get("name", ""), user_id=body.get("user_id"), mt5_login=body.get("mt5_login"),
                broker=body.get("broker", ""), account_type=body.get("account_type", "demo"),
                initial_balance=body.get("initial_balance", 0.0), currency=body.get("currency", "USD"),
                prop_profile=body.get("prop_profile"), overrides=body.get("overrides"), actor="dashboard")
            service.db.audit(int(service.clock()), "dashboard", "ACCOUNT_CREATED", {"id": acc.id, "name": acc.name})
            return {"account": acc.to_dict()}
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    @app.patch("/api/accounts/{account_id}")
    async def accounts_update(account_id: str, request: Request):
        err = auth(request)
        if err:
            return err
        body = await request.json()
        try:
            acc = service.accounts.update(account_id, actor="dashboard", **body)
            return {"account": acc.to_dict()}
        except KeyError:
            return JSONResponse({"error": "unknown account"}, status_code=404)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    @app.post("/api/accounts/{account_id}/action")
    async def accounts_action(account_id: str, request: Request):
        """activate | pause | archive | select — independent per-account controls."""
        err = auth(request)
        if err:
            return err
        body = await request.json()
        action = str(body.get("action", ""))
        try:
            if action == "select":
                if service.accounts.get(account_id) is None:
                    raise ApiError(404, "unknown account")
                service.db.kv_set("selected_account", account_id)
                return {"selected": account_id}
            if action == "activate":
                acc = service.accounts.activate(account_id, actor="dashboard")
                ea = service.accounts.ea_state(account_id)
                now = int(service.clock())
                fresh = ea.get("ts") and now - int(ea["ts"]) <= service.s.heartbeat_timeout
                ident = ea.get("ea_verified_login") is not None and (
                    acc.mt5_login is None or ea.get("ea_verified_login") == acc.mt5_login)
                service.db.audit(now, "dashboard", "ACCOUNT_ACTIVATE",
                                 {"id": account_id, "ea_fresh": bool(fresh), "identity_ok": bool(ident)})
                return {"account": acc.to_dict(), "checks": {"ea_online": bool(fresh),
                                                             "identity_verified": bool(ident)}}
            if action == "pause":
                return {"account": service.accounts.pause(account_id, actor="dashboard").to_dict()}
            if action == "archive":
                if not body.get("confirm"):
                    return JSONResponse({"error": "confirmation_required"}, status_code=409)
                service.accounts.archive(account_id, actor="dashboard")
                return {"archived": account_id}
            return JSONResponse({"error": "unknown_action"}, status_code=400)
        except (KeyError, ValueError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    @app.get("/api/positions")
    def positions(request: Request, account: Optional[str] = None):
        err = auth(request)
        if err:
            return err
        acc = get_acc(account)
        store = _store_for(service, acc.id)
        return {"baskets": [json.loads(json.dumps(b, default=str)) for b in store.baskets.values()]}

    @app.post("/api/positions/action")
    async def positions_action(request: Request):
        """close_position | close_basket | emergency_stop — destructive actions need confirm=true.

        Commands are queued for the EA (the EA remains the final execution authority); they are
        executed broker-side when the EA next heartbeats."""
        err = auth(request)
        if err:
            return err
        body = await request.json()
        action = str(body.get("action", ""))
        if action not in ("close_position", "close_basket", "emergency_stop", "close_all"):
            return JSONResponse({"error": "unknown_action"}, status_code=400)
        if not body.get("confirm"):
            return JSONResponse({"error": "confirmation_required",
                                 "detail": "destructive action requires explicit confirmation"}, status_code=409)
        acc = get_acc(body.get("account"))
        from .accounts import uuid4hex
        cid = uuid4hex()
        payload = {"action": action, "ticket": body.get("ticket"), "basket": body.get("basket"),
                   "account_id": acc.id}
        service.db.add_command(cid, int(service.clock()), "POSITION_ACTION", payload)
        service.db.audit(int(service.clock()), "dashboard", "POSITION_ACTION_QUEUED",
                         {"command_id": cid, **payload})
        return {"queued": True, "command_id": cid}

    @app.get("/api/history")
    def history(request: Request, account: Optional[str] = None, symbol: Optional[str] = None,
                result: Optional[str] = None, setup: Optional[str] = None, since: int = 0, limit: int = 200):
        err = auth(request)
        if err:
            return err
        acc = get_acc(account)
        store = _store_for(service, acc.id)
        rows = []
        for b in store.baskets.values():
            if b.status != "CLOSED":
                continue
            res = "WIN" if b.final_pnl > 0 else ("LOSS" if b.final_pnl < 0 else "NEUTRAL")
            if result and result.upper() != res:
                continue
            r_mult = (b.final_pnl / b.planned_risk_money) if b.planned_risk_money else None
            rows.append({"closed_at": b.opened_at, "account": acc.name, "symbol": b.spec.symbol,
                         "direction": "BUY" if b.direction == 1 else "SELL", "basket": b.id,
                         "risk": round(b.planned_risk_money, 2), "pnl": round(b.final_pnl, 2),
                         "r_multiple": round(r_mult, 2) if r_mult is not None else None,
                         "result": res, "setup": b.close_reason, "reason": b.close_reason})
        if symbol:
            rows = [r for r in rows if r["symbol"].upper().startswith(symbol.upper())]
        rows.sort(key=lambda r: r["closed_at"], reverse=True)
        return {"history": rows[:limit]}

    @app.get("/api/activity")
    def activity(request: Request, account: Optional[str] = None, limit: int = 60):
        """Live bot activity feed: real events with exact reasons (never just 'Trade failed')."""
        err = auth(request)
        if err:
            return err
        ev = service.db.events(limit=limit)
        au = service.db.audit_log(limit=limit)
        merged = ([{"ts": e["ts"], "kind": "EVENT", "type": e["type"], "detail": e.get("payload", {})} for e in ev]
                  + [{"ts": a["ts"], "kind": "AUDIT", "type": a["action"], "detail": a.get("detail", {})} for a in au])
        merged.sort(key=lambda x: x["ts"], reverse=True)
        return {"activity": merged[:limit]}

    @app.get("/api/settings")
    def settings_get(request: Request, account: Optional[str] = None):
        err = auth(request)
        if err:
            return err
        acc = get_acc(account)
        cfg = service.accounts.effective_config(acc, service.cfg.current)
        from .settings import SETTINGS
        return {"schema": [{"key": s.key, "label": s.label, "kind": s.kind, "choices": list(s.choices),
                            "unit": s.unit} for s in SETTINGS.values()],
                "values": json.loads(cfg.canonical_json()),
                "version": cfg.config_version,
                "applied_version": service.cfg.applied_version,
                "applied_by_ea": service.cfg.applied_version >= cfg.config_version}

    @app.post("/api/settings")
    async def settings_set(request: Request):
        """Dashboard change -> validated -> NEW config version/hash -> sent to EA -> EA acks -> Applied.
        A setting is NEVER shown as applied merely because it was saved."""
        err = auth(request)
        if err:
            return err
        body = await request.json()
        key, value = str(body.get("key", "")), str(body.get("value", ""))
        s = SETTINGS.get(key)
        if s is not None and s.kind == "toggle":
            # dashboard sends the FULL desired list ("RSI,MACD,BB"); apply as a set via propose_custom
            want = [x.strip() for x in value.replace(";", ",").split(",") if x.strip()]
            from .settings import get_path
            cur_list = get_path(service.cfg.view(), s.path)

            def mutate(data, _s=s, _want=want):
                got = [c for c in _s.choices if c.lower() in {w.lower() for w in _want}]
                bad = [w for w in _want if w.lower() not in {c.lower() for c in _s.choices}]
                if bad:
                    raise ValueError("choose from: " + ", ".join(_s.choices))
                data_set = set(got)
                lst = [c for c in _s.choices if c in data_set]
                if not lst:
                    raise ValueError("at least one must stay enabled")
                from .settings import set_path
                set_path(data, _s.path, lst)
            res = service.cfg.propose_custom("dashboard", key, f"{s.label} -> {', '.join(want)}", mutate, always_confirm=False)
        else:
            res = service.cfg.propose("dashboard", key, value)
        pid = res.proposal.id if res.proposal else None
        if pid and body.get("confirm"):
            res = service.cfg.confirm("dashboard", pid)
            pid = res.proposal.id if res.proposal else pid
        return {"ok": res.ok, "message": res.message,
                "proposal_id": pid,
                "needs_confirmation": not res.ok and ("confirm" in res.message.lower() or "again" in res.message.lower()),
                "version": service.cfg.current.config_version,
                "applied_version": service.cfg.applied_version}

    @app.get("/")
    @app.get("/index.html")
    def dashboard_index():
        p = os.path.join(dash_dir, "index.html")
        if os.path.exists(p):
            return FileResponse(p, media_type="text/html")
        return JSONResponse({"error": "dashboard files missing"}, status_code=404)

    @app.get("/app.js")
    def dashboard_js():
        p = os.path.join(dash_dir, "app.js")
        if os.path.exists(p):
            return FileResponse(p, media_type="application/javascript")
        return JSONResponse({"error": "dashboard files missing"}, status_code=404)

    @app.get("/style.css")
    def dashboard_css():
        p = os.path.join(dash_dir, "style.css")
        if os.path.exists(p):
            return FileResponse(p, media_type="text/css")
        return JSONResponse({"error": "dashboard files missing"}, status_code=404)
