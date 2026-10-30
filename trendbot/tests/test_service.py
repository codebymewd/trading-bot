import json

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.app import protocol
from backend.app.config_schema import PropProfile, SystemConfig
from backend.app.service import Service, Settings, create_app
from backend.app.settings import SETTINGS, ConfigManager, parse_value
from backend.app.db import Db
from backend.app.telegram_client import TelegramClient, poll_once

SECRET = "x" * 32
USER = 4242


class Notes:
    def __init__(self):
        self.sent = []

    def send(self, chat_id, text, keyboard=None):
        self.sent.append((chat_id, text, keyboard))

    def texts(self):
        return [t for _, t, _ in self.sent]


class Clock:
    def __init__(self, t=1_700_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def make(initial=None):
    clock, notes = Clock(), Notes()
    svc = Service(Settings(SECRET, {USER}), notes, clock, initial)
    return svc, TestClient(create_app(svc)), notes, clock


def post(client, clock, path, body, secret=SECRET, ts=None, nonce=None, tamper=False):
    raw = json.dumps(body).encode()
    h = protocol.sign(secret, "POST", path, raw, int(clock()) if ts is None else ts, nonce)
    if tamper:
        raw = raw.replace(b"1", b"2")
    return client.post(path, content=raw, headers=h)


def tg(text=None, data=None, user=USER):
    base = {"from": {"id": user}, "chat": {"id": user}}
    if data is not None:
        return {"callback_query": {"id": "cb", "from": {"id": user}, "message": {"chat": {"id": user}}, "data": data}}
    return {"message": {**base, "text": text}}


def say(svc, text=None, data=None, user=USER):
    return svc.ctl.handle(tg(text, data, user))


def confirm_id(replies):
    kb = replies[0].keyboard
    return kb[0][0][1].split(":")[1]


# ================================================================== protocol & EA endpoints
def test_unsigned_and_badly_signed_requests_are_rejected():
    svc, c, notes, clock = make()
    assert c.post("/ea/heartbeat", json={}).status_code == 401
    assert post(c, clock, "/ea/heartbeat", {}, secret="y" * 32).status_code == 401
    assert post(c, clock, "/ea/heartbeat", {"a": 1}, tamper=True).status_code == 401
    assert post(c, clock, "/ea/heartbeat", {}, ts=int(clock()) - 120).status_code == 401       # clock skew
    h = protocol.sign(SECRET, "POST", "/ea/events", b"{}", int(clock()))                       # signed for another path
    assert c.post("/ea/heartbeat", content=b"{}", headers=h).status_code == 401
    assert any(a["action"] == "AUTH_REJECTED" for a in svc.db.audit_log(20))


def test_replayed_request_is_rejected():
    svc, c, notes, clock = make()
    assert post(c, clock, "/ea/heartbeat", {}, nonce="n1").status_code == 200
    assert post(c, clock, "/ea/heartbeat", {}, nonce="n1").status_code == 401


def test_heartbeat_delivers_config_only_when_ea_is_behind():
    svc, c, notes, clock = make()
    r = post(c, clock, "/ea/heartbeat", {"applied_version": 0, "account": {"login": 1}}).json()
    assert r["config"]["config_version"] == 1 and r["config"]["config_hash"] and "derived" in r["config"]
    r = post(c, clock, "/ea/heartbeat", {"applied_version": 1}).json()
    assert "config" not in r and r["stale_config_seconds"] == 300


def test_config_change_is_applied_only_after_ea_ack():
    svc, c, notes, clock = make()
    say(svc, "/setrisk 0.5")                                   # a decrease needs no confirmation
    assert svc.cfg.current.risk.risk_percent == 0.5 and svc.cfg.applied_version == 1
    hb = post(c, clock, "/ea/heartbeat", {"applied_version": 1}).json()
    assert hb["config"]["config_version"] == 2
    cfg = SystemConfig(**{k: v for k, v in hb["config"].items() if k not in ("derived", "config_hash")})
    r = post(c, clock, "/ea/config/ack", {"version": 2, "hash": cfg.config_hash(), "ok": True}).json()
    assert "applied by the EA" in r["result"] and svc.cfg.applied_version == 2
    assert any("applied by the EA" in t for t in notes.texts())


def test_failed_or_mismatching_ack_is_never_reported_as_applied():
    svc, c, notes, clock = make()
    say(svc, "/setrisk 0.5")
    post(c, clock, "/ea/config/ack", {"version": 2, "hash": "wrong", "ok": True})
    assert svc.cfg.applied_version == 1 and any("could NOT apply" in t for t in notes.texts())
    post(c, clock, "/ea/config/ack", {"version": 2, "hash": svc.cfg.current.config_hash(), "ok": False, "error": "bad value"})
    assert svc.cfg.applied_version == 1
    assert "old version" in post(c, clock, "/ea/config/ack", {"version": 1, "hash": "x", "ok": True}).json()["result"]


def test_events_are_idempotent_and_alerts_not_duplicated():
    svc, c, notes, clock = make()
    ev = {"events": [{"event_id": "e1", "type": "BASKET_OPENED", "basket": "B1", "symbol": "XAUUSD", "direction": 1,
                      "lots": 0.02, "risk": 10.4, "sl": 1995.0, "tp": 2015.0}]}
    assert post(c, clock, "/ea/events", ev).json() == {"accepted": 1, "duplicates": 0}
    assert post(c, clock, "/ea/events", ev).json() == {"accepted": 0, "duplicates": 1}
    assert sum("Basket opened" in t for t in notes.texts()) == 1


def test_alert_throttle_and_log_only_events():
    svc, c, notes, clock = make()
    for i in range(3):                                       # same alert key repeating inside a minute
        post(c, clock, "/ea/events", {"events": [{"event_id": f"x{i}", "type": "SL_MODIFY_FAILED", "basket": "B", "ticket": 5, "retcode": "R"}]})
    assert sum("stop failed" in t for t in notes.texts()) == 1
    before = len(notes.sent)
    post(c, clock, "/ea/events", {"events": [{"event_id": "r1", "type": "SETUP_REJECTED", "reason": "spread_too_wide"}]})
    assert len(notes.sent) == before                          # rejected setups are logged, not pushed
    assert any(e["type"] == "SETUP_REJECTED" for e in svc.db.events(None, 0, 10))


def test_command_results_close_the_command_and_disconnect_alert_fires_once():
    svc, c, notes, clock = make()
    say(svc, "/pause")
    hb = post(c, clock, "/ea/heartbeat", {"applied_version": 1}).json()
    cid = hb["commands"][0]["command_id"]
    assert hb["commands"][0]["kind"] == "PAUSE"
    post(c, clock, "/ea/events", {"events": [{"event_id": "c1", "type": "COMMAND_RESULT", "command_id": cid, "kind": "PAUSE", "ok": True}]})
    assert post(c, clock, "/ea/heartbeat", {"applied_version": 1}).json()["commands"] == []
    clock.t += 100
    svc.check_ea_connection()
    svc.check_ea_connection()
    assert sum("EA disconnected" in t for t in notes.texts()) == 1
    assert sum("EA connected" in t for t in notes.texts()) == 1


def test_webhook_requires_secret():
    clock, notes = Clock(), Notes()
    svc = Service(Settings(SECRET, {USER}, webhook_secret="hook"), notes, clock)
    c = TestClient(create_app(svc))
    assert c.post("/telegram/webhook/wrong", json=tg("/start")).status_code == 404
    assert c.post("/telegram/webhook/hook", json=tg("/start")).status_code == 200
    assert any("Authorized" in t for t in notes.texts())


# ================================================================== telegram controller
def test_unauthorized_user_gets_nothing_and_is_audited():
    svc, c, notes, clock = make()
    assert say(svc, "/status", user=999) == [] and say(svc, data="ok:abc", user=999) == []
    assert svc.db.audit_log(5)[0]["action"] == "UNAUTHORIZED_ATTEMPT"


def test_risk_increase_needs_confirmation_and_shows_previous_and_new():
    svc, c, notes, clock = make()
    r = say(svc, "/setrisk 2")
    assert "1.0%" in r[0].text and "2.0%" in r[0].text and "future baskets" in r[0].text
    assert svc.cfg.current.risk.risk_percent == 1.0                  # nothing changed yet
    r = say(svc, data="ok:" + confirm_id(r))
    assert svc.cfg.current.risk.risk_percent == 2.0 and "Waiting for the EA" in r[0].text


def test_cancel_and_expiry():
    svc, c, notes, clock = make()
    pid = confirm_id(say(svc, "/setrisk 2"))
    assert "Nothing was changed" in say(svc, data="no:" + pid)[0].text and svc.cfg.current.risk.risk_percent == 1.0
    pid = confirm_id(say(svc, "/setrisk 2"))
    clock.t += 301
    assert "expired" in say(svc, data="ok:" + pid)[0].text and svc.cfg.current.risk.risk_percent == 1.0


def test_invalid_input_is_rejected_and_config_untouched():
    svc, c, notes, clock = make()
    for bad in ("-1", "0", "abc", "5000"):
        r = say(svc, "/setrisk " + bad)
        assert "Not accepted" in r[0].text or "could not" in r[0].text.lower() or "Confirm" in r[0].text
    assert svc.cfg.current.risk.risk_percent == 1.0
    assert "choose one of" in say(svc, "/mode LIVE")[0].text          # live is not selectable from Telegram
    assert "Not accepted" in say(svc, "/window 0")[0].text
    assert "Not accepted" in say(svc, "/target 99")[0].text


def test_prop_firm_limits_cannot_be_confirmed_away():
    prof = PropProfile(name="F", confirmed=True, starting_balance=1000, daily_loss_limit_percent=5.0,
                       max_drawdown_percent=10.0, safety_buffer_percent=0.5)
    svc, c, notes, clock = make(SystemConfig(prop_profiles={"F": prof}, active_prop_profile="F"))
    r = say(svc, "/setrisk 5")
    assert "Blocked" in r[0].text and r[0].keyboard is None
    r = say(svc, "/setrisk 3")                                         # allowed but large vs the 4.5% effective limit
    assert "Confirm" in r[0].text
    r2 = say(svc, data="ok:" + confirm_id(r))
    assert "once more" in r2[0].text and svc.cfg.current.risk.risk_percent == 1.0   # second confirmation required
    say(svc, data="ok:" + confirm_id(r))
    assert svc.cfg.current.risk.risk_percent == 3.0


def test_guided_input_and_toggles():
    svc, c, notes, clock = make()
    menu = say(svc, data="menu:risk")
    assert any(label.startswith("0.5") for label, _ in menu[0].keyboard[0])
    assert "Send the new value" in say(svc, data="ask:risk")[0].text
    r = say(svc, "1.5")                                                # plain text is taken as the answer
    assert "1.5%" in r[0].text
    say(svc, data="tog:ind:CCI")
    assert "CCI" in svc.cfg.current.confirmation.enabled_indicators
    say(svc, data="tog:ind:CCI")
    assert "CCI" not in svc.cfg.current.confirmation.enabled_indicators
    for name in ("RSI", "MACD", "BB"):                                 # would leave fewer indicators than the minimum of 3
        r = say(svc, data=f"tog:ind:{name}")
    assert "Not accepted" in r[0].text and len(svc.cfg.current.confirmation.enabled_indicators) >= 3


def test_mode_change_and_high_impact_news_toggle_need_confirm():
    svc, c, notes, clock = make()
    r = say(svc, "/mode DEMO")
    assert svc.cfg.current.mode.value == "SIGNAL_ONLY" and "Confirm" in r[0].text
    say(svc, data="ok:" + confirm_id(r))
    assert svc.cfg.current.mode.value == "DEMO"
    r = say(svc, data="set:news_high:on")
    assert "Confirm" in r[0].text and svc.cfg.current.news.high.allow_entries is False


def test_commands_are_queued_not_executed():
    svc, c, notes, clock = make()
    post(c, clock, "/ea/heartbeat", {"applied_version": 1, "positions": [
        {"ticket": 7, "symbol": "XAUUSD", "direction": 1, "lots": 0.02, "entry": 2000.2, "sl": 1995.0, "tp": 2015.0}]})
    r = say(svc, "/closeposition")
    assert r[0].keyboard[0][0][1] == "pos:7"
    q = say(svc, data="pos:7")
    assert "Confirm" in q[0].text and not svc.db.pending_commands()
    done = say(svc, data="ok:" + confirm_id(q))
    assert "not done until then" in done[0].text
    assert svc.db.pending_commands()[0]["kind"] == "CLOSE_POSITION" and svc.db.pending_commands()[0]["ticket"] == 7
    assert "no longer open" in say(svc, data="pos:99")[0].text
    sd = say(svc, "/shutdown")
    assert "Confirm" in sd[0].text
    say(svc, data="ok:" + confirm_id(sd))
    assert {x["kind"] for x in svc.db.pending_commands()} == {"CLOSE_POSITION", "SHUTDOWN"}


def test_watchlist_uses_real_terminal_symbols():
    svc, c, notes, clock = make()
    post(c, clock, "/ea/heartbeat", {"applied_version": 1, "symbols": [
        {"name": "XAUUSDm", "asset_class": "metals", "spec_ok": True}, {"name": "EXOTICm", "asset_class": "other", "spec_ok": False}]})
    assert "not in the terminal" in say(svc, "/addsymbol XAUUSD")[0].text           # must use the broker's real name
    assert "could not be validated" in say(svc, "/addsymbol EXOTICm")[0].text
    r = say(svc, "/addsymbol XAUUSDm")
    say(svc, data="ok:" + confirm_id(r))
    wl = svc.cfg.current.instruments.watchlist
    assert [w.symbol for w in wl] == ["XAUUSDm"] and wl[0].state.value == "MONITORED" and wl[0].broker_symbol == "XAUUSDm"
    assert "already" in say(svc, "/addsymbol XAUUSDm")[0].text
    r = say(svc, "/removesymbol XAUUSDm")
    say(svc, data="ok:" + confirm_id(r))
    assert svc.cfg.current.instruments.watchlist == []
    assert "not on the watchlist" in say(svc, "/removesymbol XAUUSDm")[0].text


def test_status_account_report_and_logs():
    svc, c, notes, clock = make()
    assert "never connected" in say(svc, "/status")[0].text
    post(c, clock, "/ea/heartbeat", {"applied_version": 1, "account": {"login": 555, "is_demo": True, "balance": 100, "equity": 100,
                                                                        "free_margin": 100, "currency": "USD"}})
    s = say(svc, "/status")[0].text
    assert "connected" in s and "555" in s and "(demo)" in s
    a = say(svc, "/account")[0].text
    assert "never need or accept passwords" in a and "demo" in a
    post(c, clock, "/ea/events", {"events": [
        {"event_id": "b1", "type": "BASKET_CLOSED", "basket": "B1", "reason": "target", "net": 30.0, "outcome": "WIN"},
        {"event_id": "b2", "type": "BASKET_CLOSED", "basket": "B2", "reason": "stop_or_target", "net": -10.0, "outcome": "LOSS"}]})
    rep = say(svc, "/report daily")[0].text
    assert "2 baskets" in rep and "1 wins" in rep and "net +20.00" in rep
    assert "target" in say(svc, "/history")[0].text and "BASKET_CLOSED" in say(svc, "/logs")[0].text
    assert "Use /report" in say(svc, "/report yearly")[0].text


def test_settings_menu_covers_the_required_controls():
    svc, c, notes, clock = make()
    labels = {l for row in say(svc, "/settings")[0].keyboard for l, _ in row}
    for needed in ("Risk per basket", "Target multiplier", "Scalp window", "Positions per basket", "Indicators on/off",
                   "Minimum indicator confirmations", "Break-even method", "Break-even trigger", "Break-even offset",
                   "Timeout action", "Volatility states allowed", "Trade during high-impact news"):
        assert needed in labels


def test_no_command_can_disable_all_risk_controls():
    svc, c, notes, clock = make()
    assert "Unknown command" in say(svc, "/disablerisk")[0].text
    assert "Unknown command" in say(svc, "/live")[0].text
    assert "Not accepted" in say(svc, "/setrisk 0")[0].text


# ================================================================== settings + client
def test_parse_value_and_stale_proposal_detection():
    assert parse_value(SETTINGS["risk"], "2%", 1.0) == 2.0
    assert parse_value(SETTINGS["window"], "15", 5) == 15
    assert parse_value(SETTINGS["be"], "b", "off") == "B"
    assert parse_value(SETTINGS["news_high"], "ON", False) is True
    assert parse_value(SETTINGS["ind"], "cci", ["RSI"]) == ["RSI", "CCI"]
    assert parse_value(SETTINGS["daily_loss"], "off", 3.0) is None
    with pytest.raises(ValueError):
        parse_value(SETTINGS["timeout"], "HOLD", "CLOSE_ALL")
    m = ConfigManager(Db())
    p = m.propose("u", "risk", "2").proposal
    m.propose("u", "window", "30")                                        # applies immediately, bumping the version
    assert "changed in the meantime" in m.confirm("u", p.id).message


def test_telegram_client_and_polling():
    seen = []

    def handler(request: httpx.Request):
        seen.append((request.url.path, request.read().decode()))
        if request.url.path.endswith("getUpdates"):
            return httpx.Response(200, json={"ok": True, "result": [
                {"update_id": 10, "message": {"from": {"id": 1}, "chat": {"id": 1}, "text": "/start"}},
                {"update_id": 11, "callback_query": {"id": "c1", "from": {"id": 1}, "data": "x"}}]})
        return httpx.Response(200, json={"ok": True})
    client = TelegramClient("TOKEN123", httpx.Client(transport=httpx.MockTransport(handler)))
    client.send(5, "hello", [[("A", "a:1")]])
    body = json.loads(seen[0][1])
    assert seen[0][0].endswith("/botTOKEN123/sendMessage") and body["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "a:1"
    assert "TOKEN123" not in seen[0][1]                                   # the token is only in the URL path
    got = []
    assert poll_once(client, got.append, 0) == 12 and len(got) == 2
    assert any(p.endswith("answerCallbackQuery") for p, _ in seen)
