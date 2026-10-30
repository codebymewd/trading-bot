"""FastAPI service: signed EA endpoints + optional Telegram webhook.

EA endpoints (all HMAC-signed, replay-protected):
  POST /ea/heartbeat   EA state in, newer configuration + pending commands out
  POST /ea/events      idempotent event upload (de-duplicated by event_id) -> Telegram alerts
  POST /ea/config/ack  the EA confirms (or refuses) a configuration version
Secrets come from environment variables (see run_server.py); they are never logged or sent to Telegram.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Protocol, Set

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import protocol
from .config_schema import SystemConfig
from .db import Db
from .settings import ConfigManager
from .telegram_ctl import AlertThrottle, Controller, Reply, format_event


class Notifier(Protocol):
    def send(self, chat_id: int, text: str, keyboard=None) -> None: ...


@dataclass
class Settings:
    ea_secret: str
    allowed_users: Set[int]
    db_path: str = ":memory:"
    webhook_secret: str = ""
    heartbeat_timeout: int = 45


class Service:
    def __init__(self, settings: Settings, notifier: Optional[Notifier] = None, clock: Callable[[], float] = time.time,
                 initial: Optional[SystemConfig] = None):
        self.s, self.clock = settings, clock
        self.db = Db(settings.db_path)
        self.cfg = ConfigManager(self.db, initial, clock)
        self.ctl = Controller(self.db, self.cfg, settings.allowed_users, clock)
        self.notifier = notifier
        self.throttle = AlertThrottle(clock)
        self.ea_online: Optional[bool] = None

    # ---- outbound
    def notify(self, text: str, key: Optional[str] = None) -> None:
        if self.notifier is None:
            return
        if key is not None and not self.throttle.allow(key):
            return
        for uid in self.s.allowed_users:
            self.notifier.send(uid, text)

    def send_replies(self, replies: List[Reply]) -> None:
        if self.notifier:
            for r in replies:
                self.notifier.send(r.chat_id, r.text, r.keyboard)

    # ---- EA handlers
    def heartbeat(self, body: dict) -> dict:
        now = int(self.clock())
        was = self.ea_online
        self.ea_online = True
        state = {k: body[k] for k in ("account", "mode", "state", "positions", "symbols", "gate", "news", "volatility", "applied_version")
                 if body.get(k) is not None}
        state["ts"] = now
        self.db.kv_set("ea_state", state)
        if was is not True:
            self.event_alert({"type": "EA_CONNECTED"})
        resp = {"server_time": now, "commands": self.db.pending_commands(),
                "stale_config_seconds": self.cfg.current.safety.stale_config_seconds}
        applied = int(body.get("applied_version") or 0)
        if applied < self.cfg.current.config_version:
            resp["config"] = self.cfg.current.ea_payload()
        return resp

    def check_ea_connection(self) -> None:
        """Call periodically: raises ONE disconnect alert when heartbeats stop."""
        st = self.db.kv_get("ea_state") or {}
        if st.get("ts") is not None and self.ea_online and self.clock() - st["ts"] > self.s.heartbeat_timeout:
            self.ea_online = False
            self.event_alert({"type": "EA_DISCONNECTED"})

    def events(self, body: dict) -> dict:
        new = dup = 0
        for e in body.get("events", []):
            eid = str(e.get("event_id") or "")
            typ = str(e.get("type") or "")
            if not eid or not typ:
                continue
            if self.db.add_event(eid, int(self.clock()), typ, {k: v for k, v in e.items() if k not in ("event_id", "type")}):
                new += 1
                if typ == "COMMAND_RESULT" and e.get("command_id"):
                    self.db.set_command_status(str(e["command_id"]), "done" if e.get("ok") else "failed")
                self.event_alert(e)
            else:
                dup += 1
        return {"accepted": new, "duplicates": dup}

    def event_alert(self, e: dict) -> None:
        f = format_event(e)
        if f:
            self.notify(f[1], f[0])

    def config_ack(self, body: dict) -> dict:
        msg = self.cfg.on_ack(int(body.get("version", 0)), str(body.get("hash", "")), bool(body.get("ok")), str(body.get("error", "")))
        self.notify(msg, key="ack|" + msg)      # a repeating refusal must not flood Telegram
        return {"result": msg}

    def telegram_update(self, update: dict) -> None:
        self.send_replies(self.ctl.handle(update))


def create_app(service: Service) -> FastAPI:
    app = FastAPI(title="TrendBot backend", docs_url=None, redoc_url=None)
    app.state.service = service

    async def verified(request: Request, path: str):
        body = await request.body()
        ok, why = protocol.verify(service.s.ea_secret, request.method, path, body, request.headers, int(service.clock()), service.db)
        if not ok:
            service.db.audit(int(service.clock()), "ea", "AUTH_REJECTED", {"path": path, "why": why})
            return None, JSONResponse({"error": why}, status_code=401)
        try:
            return json.loads(body or b"{}"), None
        except ValueError:
            return None, JSONResponse({"error": "bad_json"}, status_code=400)

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.post("/ea/heartbeat")
    async def heartbeat(request: Request):
        body, err = await verified(request, "/ea/heartbeat")
        return err or service.heartbeat(body)

    @app.post("/ea/events")
    async def events(request: Request):
        body, err = await verified(request, "/ea/events")
        return err or service.events(body)

    @app.post("/ea/config/ack")
    async def ack(request: Request):
        body, err = await verified(request, "/ea/config/ack")
        return err or service.config_ack(body)

    @app.post("/telegram/webhook/{secret}")
    async def webhook(secret: str, request: Request):
        if not service.s.webhook_secret or secret != service.s.webhook_secret:
            return JSONResponse({"error": "not_found"}, status_code=404)
        service.telegram_update(await request.json())
        return {"ok": True}

    return app
