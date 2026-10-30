"""Telegram controller. Pure logic (no network): `handle(update)` returns replies, so every command is testable.

Rules enforced here:
- Only allow-listed Telegram user ids are served; everyone else is silently ignored (and audited).
- Telegram can never bypass the EA or the prop-firm guard: it only proposes configuration changes (validated by the
  schema, confirmed by the user, and reported as applied only after the EA's acknowledgement) and queues commands.
- Passwords/credentials are never requested or stored. There is no command that disables all risk controls.
"""
from __future__ import annotations

import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set, Tuple

from .config_schema import InstrumentConfig
from .db import Db
from .news import asset_class_of
from .prop_rules import ProfileDraft, parse_rules
from .settings import SETTINGS, ConfigManager, Proposal, _fmt, get_path
from .trading_day import resolve_fixed_offset, trading_day_window_utc

Keyboard = List[List[Tuple[str, str]]]


@dataclass
class Reply:
    chat_id: int
    text: str
    keyboard: Optional[Keyboard] = None


class AlertThrottle:
    """De-duplicates alerts (same key inside `window` seconds) and caps the number per minute."""

    def __init__(self, clock: Callable[[], float] = time.time, window: int = 60, per_minute: int = 20):
        self.clock, self.window, self.per_minute = clock, window, per_minute
        self.last: Dict[str, float] = {}
        self.sent: List[float] = []
        self.suppressed = 0

    def allow(self, key: str) -> bool:
        now = self.clock()
        self.sent = [t for t in self.sent if now - t < 60]
        if key in self.last and now - self.last[key] < self.window:
            self.suppressed += 1
            return False
        if len(self.sent) >= self.per_minute:
            self.suppressed += 1
            return False
        self.last[key] = now
        self.sent.append(now)
        return True


HELP = (
    "Commands\n/status  /markets  /positions  /history  /report [daily|weekly|monthly]\n"
    "/risk  /setrisk <%>  /target <x>  /window <min>  /trades <n>  /mode\n/news  /volatility  /rules  /importrules\n"
    "/symbols  /watchlist  /addsymbol <name>  /removesymbol <name>  /instrument <name>  /instrumentreport  /scan\n"
    "/pause  /resume  /closeposition  /shutdown  /kill\n/settings  /logs  /account\n"
    "Money-affecting changes ask you to confirm first. I never ask for your broker password.")


class Controller:
    def __init__(self, db: Db, cfg: ConfigManager, allowed_users: Set[int], clock: Callable[[], float] = time.time):
        self.db, self.cfg, self.allowed, self.clock = db, cfg, set(allowed_users), clock
        self.pending_input: Dict[int, str] = {}
        self.actions: Dict[str, dict] = {}
        self.drafts: Dict[int, ProfileDraft] = {}

    # ------------------------------------------------------------------ entry point
    def handle(self, update: dict) -> List[Reply]:
        cb = update.get("callback_query")
        msg = update.get("message") or (cb or {}).get("message") or {}
        user = (cb or update.get("message") or {}).get("from", {}).get("id")
        chat = (msg.get("chat") or {}).get("id", user)
        if user not in self.allowed:
            self.db.audit(int(self.clock()), str(user), "UNAUTHORIZED_ATTEMPT", {"chat": chat})
            return []                                         # never reveal that a bot exists to strangers
        if cb:
            return self._callback(user, chat, cb.get("data", ""))
        text = (update.get("message") or {}).get("text", "").strip()
        if not text:
            return []
        if text.startswith("/"):
            self.pending_input.pop(user, None)
            parts = text.split()
            return self._command(user, chat, parts[0].lstrip("/").split("@")[0].lower(), parts[1:])
        if user in self.pending_input:
            key = self.pending_input.pop(user)
            if key == "__rules__":
                return self._rules_text(user, chat, text)
            return self._propose(user, chat, key, text)
        return [Reply(chat, "Send /help for the list of commands.")]

    # ------------------------------------------------------------------ helpers
    def state(self) -> dict:
        st = self.db.kv_get("ea_state", {}) or {}
        return {k: v for k, v in st.items() if v is not None}

    def _cfgv(self) -> dict:
        return self.cfg.view()

    def _propose(self, user, chat, key, text) -> List[Reply]:
        r = self.cfg.propose(str(user), key, text)
        if not r.ok:
            return [Reply(chat, r.message)]
        if r.proposal and r.proposal.needs_confirm and not r.applied_now:
            return [Reply(chat, r.message + ("\n\nConfirm?" if r.proposal.stage == 1 else ""), self._confirm_kb(r.proposal.id))]
        return [Reply(chat, r.message)]

    @staticmethod
    def _confirm_kb(pid: str) -> Keyboard:
        return [[("Confirm", f"ok:{pid}"), ("Cancel", f"no:{pid}")]]

    def _setting_menu(self, chat, key) -> List[Reply]:
        s = SETTINGS[key]
        cur = get_path(self._cfgv(), s.path)
        rows: Keyboard = []
        if s.kind == "toggle":
            rows.append([((f"{'✔' if c in cur else '✘'} {c}"), f"tog:{key}:{c}") for c in s.choices])
        elif s.kind == "bool":
            rows.append([("On", f"set:{key}:on"), ("Off", f"set:{key}:off")])
        elif s.kind == "enum" and s.key != "profile":
            rows.append([(c, f"set:{key}:{c}") for c in s.choices])
        elif s.presets:
            rows.append([(f"{p}{s.unit if s.unit in ('%', 'x') else ''}", f"set:{key}:{p}") for p in s.presets])
        if s.kind in ("float", "int"):
            rows.append([("Custom value", f"ask:{key}")])
        if s.key == "profile":
            rows.append([(p, f"set:{key}:{p}") for p in self.cfg.current.prop_profiles] or [("(no saved profiles)", "noop")])
        return [Reply(chat, f"{s.label}\nCurrent: {_fmt(cur, s.unit)}\nApplied by the EA: version {self.cfg.applied_version}"
                            f" (requested {self.cfg.current.config_version})", rows)]

    # ------------------------------------------------------------------ callbacks
    def _callback(self, user, chat, data) -> List[Reply]:
        kind, _, rest = data.partition(":")
        if kind == "menu":
            return self._setting_menu(chat, rest) if rest in SETTINGS else self._command(user, chat, "settings", [])
        if kind in ("set", "tog"):
            key, _, val = rest.partition(":")
            return self._propose(user, chat, key, val)
        if kind == "ask":
            self.pending_input[user] = rest
            return [Reply(chat, f"Send the new value for {SETTINGS[rest].label} ({SETTINGS[rest].unit or 'number'}).")]
        if kind == "ok":
            return self._confirm(user, chat, rest)
        if kind == "no":
            self.actions.pop(rest, None)
            return [Reply(chat, self.cfg.cancel(str(user), rest).message)]
        if kind == "pos":
            return self._ask_close(user, chat, rest)
        return []

    def _confirm(self, user, chat, pid) -> List[Reply]:
        act = self.actions.pop(pid, None)
        if act is not None:
            if self.clock() - act["created"] > 300:
                return [Reply(chat, "That request expired. Please ask again.")]
            cid = uuid.uuid4().hex[:12]
            self.db.add_command(cid, int(self.clock()), act["kind"], act["payload"])
            self.db.audit(int(self.clock()), str(user), "COMMAND_QUEUED", {"kind": act["kind"], "payload": act["payload"], "id": cid})
            return [Reply(chat, f"{act['label']} requested. I will report back when the EA has done it (it is not done until then).")]
        r = self.cfg.confirm(str(user), pid)
        if r.ok and r.proposal and r.proposal.stage == 2 and not r.applied_now:
            return [Reply(chat, r.message, self._confirm_kb(pid))]
        return [Reply(chat, r.message)]

    def _queue_confirm(self, chat, kind, payload, label, prompt) -> List[Reply]:
        pid = uuid.uuid4().hex[:10]
        self.actions[pid] = {"kind": kind, "payload": payload, "label": label, "created": self.clock()}
        return [Reply(chat, prompt + "\n\nConfirm?", self._confirm_kb(pid))]

    def _ask_close(self, user, chat, ticket) -> List[Reply]:
        pos = [p for p in self.state().get("positions", []) if str(p.get("ticket")) == str(ticket)]
        if not pos:
            return [Reply(chat, "That position is no longer open.")]
        p = pos[0]
        return self._queue_confirm(chat, "CLOSE_POSITION", {"ticket": int(ticket)}, "Close position",
                                   f"Close {p.get('symbol')} {p.get('lots')} lots (ticket {ticket}) now?")

    # ------------------------------------------------------------------ commands
    def _command(self, user, chat, cmd, args) -> List[Reply]:
        st = self.state()
        cv = self._cfgv()
        r = lambda text, kb=None: [Reply(chat, text, kb)]
        quick = {"setrisk": "risk", "risk": "risk", "target": "target", "rr": "target", "window": "window",
                 "trades": "trades_day", "mode": "mode", "kill": "kill"}
        if cmd in ("start", "help"):
            return r(("Authorized. " if cmd == "start" else "") + HELP)
        if cmd in quick:
            key = quick[cmd]
            if args:
                return self._propose(user, chat, key, " ".join(args))
            return self._setting_menu(chat, key)
        if cmd == "settings":
            keys = list(SETTINGS)
            rows = [[(SETTINGS[k].label, f"menu:{k}") for k in keys[i:i + 2]] for i in range(0, len(keys), 2)]
            return r("Settings", rows)
        if cmd == "status":
            return r(self._status(st))
        if cmd == "account":
            a = st.get("account")
            if not a:
                return r("No account data yet: the EA has not connected.")
            return r(f"Account {a.get('login')} ({'demo' if a.get('is_demo') else 'REAL'}) balance {a.get('balance')} "
                     f"equity {a.get('equity')} free margin {a.get('free_margin')} {a.get('currency', '')}\n"
                     f"Allowed accounts: {cv['broker']['allowed_logins'] or 'none set'}\n"
                     "Log in to accounts in MT5 itself. I never need or accept passwords.")
        if cmd == "positions":
            pos = st.get("positions", [])
            if not pos:
                return r("No open positions.")
            lines = [f"{p['symbol']} {'BUY' if p['direction'] == 1 else 'SELL'} {p['lots']} @ {p['entry']} SL {p['sl']} TP {p['tp']} P/L {p.get('profit', '?')}" for p in pos]
            return r("\n".join(lines))
        if cmd == "closeposition":
            pos = st.get("positions", [])
            if not pos:
                return r("No open positions.")
            return r("Which position?", [[(f"{p['symbol']} {p['lots']} (#{p['ticket']})", f"pos:{p['ticket']}")] for p in pos])
        if cmd in ("pause", "resume"):
            cid = uuid.uuid4().hex[:12]
            self.db.add_command(cid, int(self.clock()), cmd.upper(), {})
            self.db.audit(int(self.clock()), str(user), "COMMAND_QUEUED", {"kind": cmd.upper(), "id": cid})
            if cmd == "pause":
                return r("Pause requested: new entries will be blocked once the EA confirms. Open baskets keep their stops and management.")
            return r("Resume requested. The EA re-checks every safety condition first and may refuse; I will tell you.")
        if cmd == "shutdown":
            return self._queue_confirm(chat, "SHUTDOWN", {}, "Shutdown",
                                       "Disable new entries and stop the EA? Open positions keep their broker-side stops.")
        if cmd == "history":
            ev = self.db.events(["BASKET_CLOSED"], 0, 10)
            return r("\n".join(f"{_ts(e['ts'])} {e.get('reason')} net {e.get('net'):+.2f} ({e.get('outcome')})" for e in ev) or "No closed baskets yet.")
        if cmd == "report":
            return r(self._report((args or ["daily"])[0].lower()))
        if cmd == "logs":
            ev = self.db.events(None, 0, 12)
            lines = [f"{_ts(e['ts'])} {e['type']} {e.get('reason', e.get('basket', ''))}" for e in ev]
            return r("\n".join(lines) or "No events yet.")
        if cmd in ("markets", "symbols", "watchlist", "instrumentreport", "scan", "instrument"):
            return self._instruments(user, chat, cmd, args, st, cv)
        if cmd in ("addsymbol", "removesymbol"):
            return self._edit_watchlist(user, chat, cmd, args, st)
        if cmd == "news":
            return r(self._news(st, cv))
        if cmd == "volatility":
            v = st.get("volatility", {})
            rej = [e for e in self.db.events(["SETUP_REJECTED"], 0, 50) if str(e.get("reason", "")).startswith("volatility_")]
            return r(f"Allowed states: {', '.join(cv['volatility']['allowed_states'])}\nCurrent: {v or 'no data yet'}\nRecent volatility rejections: {len(rej)}")
        if cmd == "rules":
            p = cv["active_prop_profile"]
            if not p:
                return r("No active prop-firm profile. Saved: " + (", ".join(cv["prop_profiles"]) or "none"))
            d = cv["prop_profiles"][p]
            return r(f"Active profile {p} (confirmed: {d['confirmed']})\nDaily loss {d['daily_loss_limit_percent']}%  max drawdown {d['max_drawdown_percent']}% "
                     f"({d['drawdown_type']}, {d['drawdown_basis']})  buffer {d['safety_buffer_percent']}%\nRestricted: {d['restricted_symbols'] or 'none'}  max lot {d['max_lot_size']}")
        if cmd == "importrules":
            self.pending_input[user] = "__rules__"
            return r("Paste the prop firm's rules as ONE text message now. I extract the values, show them to you to edit "
                     "(/ruleset <field> <value>) and confirm (/ruleconfirm <profile name>), and mark anything missing or unclear. "
                     "Screenshots/PDFs need OCR on the server; until that is set up, paste the text. Nothing becomes active until you confirm.")
        if cmd in ("ruleset", "rulename", "ruleconfirm", "ruledraft"):
            return self._rules_cmd(user, chat, cmd, args)
        return r("Unknown command. /help lists what I can do.")

    # ------------------------------------------------------------------ prop-firm rule import
    def _rules_text(self, user, chat, text) -> List[Reply]:
        draft = ProfileDraft("Imported", parse_rules(text))
        self.drafts[user] = draft
        self.db.audit(int(self.clock()), str(user), "RULES_IMPORTED", {"fields": sorted(draft.values)})
        return [Reply(chat, draft.summary())]

    def _rules_cmd(self, user, chat, cmd, args) -> List[Reply]:
        d = self.drafts.get(user)
        if d is None:
            return [Reply(chat, "No draft yet. Use /importrules first.")]
        if cmd == "ruledraft":
            return [Reply(chat, d.summary())]
        if cmd == "rulename":
            d.name = " ".join(args)[:40] or d.name
            return [Reply(chat, f"Draft named '{d.name}'.")]
        if cmd == "ruleset":
            if len(args) < 2:
                return [Reply(chat, "Usage: /ruleset <field> <value>   e.g. /ruleset daily_reset_timezone Africa/Lagos")]
            err = d.set_field(args[0], " ".join(args[1:]))
            return [Reply(chat, err or ("Set.\n" + d.summary()))]
        name = " ".join(args)[:40]
        if name:
            d.name = name
        if d.unresolved:
            return [Reply(chat, "Cannot confirm yet. Still missing or unclear: " + ", ".join(d.unresolved))]
        profile = d.to_profile()
        if d.name in self.cfg.current.prop_profiles:
            return [Reply(chat, "A profile with that name already exists. Pick another with /rulename.")]

        def mutate(cfgd):
            cfgd["prop_profiles"][d.name] = profile.model_dump(mode="json")
        res = self.cfg.propose_custom(str(user), "prop_profile", f"Save confirmed prop-firm profile '{d.name}' (activate it later in Settings)", mutate)
        if not res.ok:
            return [Reply(chat, res.message)]
        return [Reply(chat, res.message + "\nYou have reviewed every value. Confirm?", self._confirm_kb(res.proposal.id))]

    # ------------------------------------------------------------------ pieces
    def _status(self, st: dict) -> str:
        now = self.clock()
        hb = st.get("ts")
        online = hb is not None and now - hb < 3 * self.cfg.current.safety.heartbeat_seconds + 5
        a = st.get("account", {})
        flags = st.get("state", {})
        return (f"EA: {'connected' if online else 'DISCONNECTED' if hb else 'never connected'}"
                f"{'' if not hb else f' (last heartbeat {int(now - hb)}s ago)'}\n"
                f"Mode: {self.cfg.current.mode.value}   paused: {flags.get('paused', '?')}   locked out: {flags.get('locked_out', '?')}\n"
                f"Account: {a.get('login', '?')} {'(demo)' if a.get('is_demo') else '(REAL)' if a else ''}  equity {a.get('equity', '?')}\n"
                f"Config: requested v{self.cfg.current.config_version}, applied by EA v{self.cfg.applied_version}\n"
                f"Open positions: {len(st.get('positions', []))}   Trend gate: {st.get('gate', 'n/a')}")

    def _report(self, period: str) -> str:
        now = int(self.clock())
        if period == "daily":
            off = resolve_fixed_offset(self.cfg.current.daily.reset_timezone) or 0
            since = int(trading_day_window_utc(__import__("datetime").datetime.fromtimestamp(now, tz=__import__("datetime").timezone.utc),
                                               off, self.cfg.current.daily.reset_time)[0].timestamp())
        elif period == "weekly":
            since = now - 7 * 86400
        elif period == "monthly":
            since = now - 30 * 86400
        else:
            return "Use /report daily, weekly or monthly."
        ev = self.db.events(["BASKET_CLOSED"], since, 1000)
        net = sum(e.get("net", 0.0) for e in ev)
        w = sum(1 for e in ev if e.get("outcome") == "WIN")
        l = sum(1 for e in ev if e.get("outcome") == "LOSS")
        return f"{period.title()} report: {len(ev)} baskets, {w} wins, {l} losses, net {net:+.2f}\n(Past results do not predict future results.)"

    def _news(self, st: dict, cv: dict) -> str:
        n = st.get("news", {})
        pol = cv["news"]
        high = pol["high"]
        if high["allow_entries"]:
            high_txt = "allow (extra checks)"
        else:
            high_txt = "block (%d min before, %d after)" % (high["pre_window_min"], high["post_window_min"])
        low_txt = "allow" if pol["low"]["allow_entries"] else "block"
        med_txt = "allow + extra checks" if pol["medium"]["allow_entries"] else "block"
        return ("News status: %s\nPolicy: low %s, medium %s, high %s\nIf news data is missing: %s\nUpcoming: %s"
                % (n.get("status", "no data from the EA yet"), low_txt, med_txt, high_txt,
                   pol["on_data_unavailable"], n.get("upcoming", "n/a")))

    def _instruments(self, user, chat, cmd, args, st, cv) -> List[Reply]:
        syms = st.get("symbols", [])
        wl = cv["instruments"]["watchlist"]
        if cmd in ("symbols", "markets"):
            if not syms:
                return [Reply(chat, "The EA has not reported the broker's symbols yet.")]
            wl_names = {w["symbol"] for w in wl}
            lines = [f"{s['name']} {s.get('asset_class', '')} spec_ok={s.get('spec_ok')}{' [watchlist]' if s['name'] in wl_names else ''}" for s in syms[:40]]
            return [Reply(chat, f"{len(syms)} symbols from the terminal (first {len(lines)}):\n" + "\n".join(lines))]
        if cmd == "watchlist":
            return [Reply(chat, "\n".join(f"{w['symbol']}  {w['state']}  {'enabled' if w['enabled'] else 'disabled'}" for w in wl) or "Watchlist is empty. Use /addsymbol <name>.")]
        if cmd in ("instrument", "instrumentreport"):
            name = args[0] if args else None
            found = [s for s in syms if name and s["name"].lower() == name.lower()]
            if not found:
                return [Reply(chat, "Usage: /instrument <symbol as shown by /symbols>")]
            s = found[0]
            return [Reply(chat, "\n".join(f"{k}: {v}" for k, v in s.items()))]
        if cmd == "scan":
            ok = [w["symbol"] for w in wl if w["enabled"] and w["state"] in ("MONITORED", "DEMO_ELIGIBLE", "LIVE_VALIDATED")]
            return [Reply(chat, f"Monitoring {len(ok)} symbols: {', '.join(ok) or 'none'}. Setups appear as alerts and in /logs.")]
        return []

    def _edit_watchlist(self, user, chat, cmd, args, st) -> List[Reply]:
        if not args:
            return [Reply(chat, f"Usage: /{cmd} <symbol>")]
        name = args[0]
        syms = {s["name"].lower(): s for s in st.get("symbols", [])}
        if cmd == "addsymbol":
            s = syms.get(name.lower())
            if s is None:
                return [Reply(chat, "That symbol is not in the terminal's list. Use /symbols to see the real names (they carry broker suffixes).")]
            if not s.get("spec_ok"):
                return [Reply(chat, "Its specifications could not be validated, so it cannot be added (unknown instruments default to NO TRADE).")]

            def mutate(d):
                wl = d["instruments"]["watchlist"]
                if any(w["symbol"].lower() == name.lower() for w in wl):
                    raise ValueError("already on the watchlist")
                wl.append(InstrumentConfig(symbol=s["name"], broker_symbol=s["name"], asset_class=s.get("asset_class") or asset_class_of(s["name"]),
                                           state="MONITORED").model_dump(mode="json"))
            res = self.cfg.propose_custom(str(user), "watchlist", f"Add {s['name']} to the watchlist (monitored; demo/live need validation)", mutate)
        else:
            def mutate(d):
                before = len(d["instruments"]["watchlist"])
                d["instruments"]["watchlist"] = [w for w in d["instruments"]["watchlist"] if w["symbol"].lower() != name.lower()]
                if len(d["instruments"]["watchlist"]) == before:
                    raise ValueError("not on the watchlist")
            res = self.cfg.propose_custom(str(user), "watchlist", f"Remove {name} from the watchlist", mutate)
        if not res.ok:
            return [Reply(chat, res.message)]
        return [Reply(chat, res.message + "\n\nConfirm?", self._confirm_kb(res.proposal.id))]


def _ts(ts: int) -> str:
    import datetime
    return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).strftime("%m-%d %H:%M")


# ------------------------------------------------------------------ EA event -> Telegram alert text
def format_event(e: dict) -> Optional[Tuple[str, str]]:
    """(dedupe_key, text) for events worth an alert; None for log-only events (e.g. every rejected setup)."""
    t = e.get("type")
    b = e.get("basket", e.get("ticket", ""))
    if t == "BASKET_OPENED":
        d = "BUY" if e.get("direction") == 1 else "SELL"
        return (f"{t}{b}", f"Basket opened: {e.get('symbol')} {d} {e.get('lots')} lots, risk {e.get('risk'):.2f}, SL {e.get('sl')}, TP {e.get('tp')}")
    if t == "BASKET_CLOSED":
        return (f"{t}{b}", f"Basket closed ({e.get('reason')}): net {e.get('net'):+.2f} - {e.get('outcome')}")
    if t == "SL_MOVED":
        return (f"{t}{b}", f"Stop moved ({e.get('reason')}) on {e.get('moved')} of {e.get('requested')} positions (confirmed by the broker).")
    if t == "SL_MODIFY_FAILED":
        return (f"{t}{b}{e.get('ticket')}", f"WARNING: moving the stop failed (ticket {e.get('ticket')}, retcode {e.get('retcode')}). It is NOT moved; the EA will retry.")
    if t in ("CLOSE_FAILED", "PARTIAL_CLOSE_FAILED"):
        return (f"{t}{b}", f"WARNING: closing failed (ticket {e.get('ticket')}, retcode {e.get('retcode')}). Retrying.")
    if t == "ORDER_REJECTED":
        return (f"{t}{b}", f"Broker rejected an order ({e.get('retcode')}): {e.get('reason')}")
    if t == "SLIPPAGE_RISK_BREACH":
        return (f"{t}{b}", "Slippage pushed risk over the limit: the basket was closed immediately.")
    if t == "BASKET_PARTIAL":
        return (f"{t}{b}", f"Only {e.get('opened')} of {e.get('planned')} positions opened; risk was recalculated downward.")
    if t == "LOCKOUT_ACTIVE":
        return (t, "Lockout: no new baskets until the next daily reset (Lagos midnight).")
    if t == "UNKNOWN_POSITION":
        return (f"{t}{b}", f"Unknown position found ({e.get('symbol')} #{e.get('ticket')}). I will not manage it - please check MT5.")
    if t == "STATE_CORRUPT":
        return (t, "The saved state file was unreadable: trading is locked until you review it.")
    if t == "COMMAND_RESULT":
        return (f"{t}{e.get('command_id')}", f"{e.get('kind')}: {'done' if e.get('ok') else 'REFUSED/failed'} {e.get('detail', '')}")
    if t in ("EA_CONNECTED", "EA_DISCONNECTED"):
        return (t, "EA connected." if t == "EA_CONNECTED" else "EA disconnected (no heartbeat). Existing broker-side stops stay in place.")
    if t == "PROP_THRESHOLD":
        return (t + str(e.get("rule")), f"Prop-firm safety threshold {e.get('state')}: {e.get('rule')}")
    if t in ("NEWS_RESTRICTION_ON", "NEWS_RESTRICTION_OFF"):
        return (t, "News restriction active." if t.endswith("ON") else "News restriction lifted.")
    if t == "MARGIN_REJECTED":
        return (t, f"Setup skipped: insufficient margin ({e.get('detail', '')}).")
    if t == "DATA_MISSING":
        return (f"{t}{e.get('what')}", f"Missing data: {e.get('what')}. New entries are blocked while it is unavailable.")
    if t == "DAILY_SUMMARY":
        return (t, f"Daily summary: {e.get('baskets', 0)} baskets, net {e.get('net', 0):+.2f}")
    if t == "SETUP_QUALIFIED":
        return (f"{t}{b}", f"Setup qualified: {e.get('symbol')} {e.get('direction')} ({e.get('kind')}) - checks running.")
    return None
