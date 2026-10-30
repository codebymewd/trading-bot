"""Telegram Bot API client (httpx) and a long-polling runner. The token comes from the environment only."""
from __future__ import annotations

import threading
from typing import Callable, List, Optional

import httpx


class TelegramClient:
    def __init__(self, token: str, http: Optional[httpx.Client] = None, base: str = "https://api.telegram.org"):
        self._url = f"{base}/bot{token}"          # never logged
        self.http = http or httpx.Client(timeout=40)

    def send(self, chat_id: int, text: str, keyboard=None) -> None:
        payload = {"chat_id": chat_id, "text": text[:4000]}
        if keyboard:
            payload["reply_markup"] = {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in keyboard]}
        self.http.post(f"{self._url}/sendMessage", json=payload)

    def get_me(self) -> dict:
        return self.http.get(f"{self._url}/getMe").json()

    def set_commands(self, commands: List[tuple]) -> bool:
        r = self.http.post(f"{self._url}/setMyCommands",
                           json={"commands": [{"command": c, "description": d[:200]} for c, d in commands]})
        return bool(r.json().get("ok"))

    def answer_callback(self, callback_id: str) -> None:
        self.http.post(f"{self._url}/answerCallbackQuery", json={"callback_query_id": callback_id})

    def get_updates(self, offset: int, timeout: int = 30) -> List[dict]:
        r = self.http.get(f"{self._url}/getUpdates", params={"offset": offset, "timeout": timeout})
        data = r.json()
        return data.get("result", []) if data.get("ok") else []


def poll_once(client: TelegramClient, handle: Callable[[dict], None], offset: int) -> int:
    for u in client.get_updates(offset, timeout=0):
        offset = max(offset, u["update_id"] + 1)
        cb = u.get("callback_query")
        if cb:
            try:
                client.answer_callback(cb["id"])
            except Exception:
                pass
        handle(u)
    return offset


def run_polling(client: TelegramClient, handle: Callable[[dict], None], stop: threading.Event) -> None:
    offset = 0
    while not stop.is_set():
        try:
            offset = poll_once(client, handle, offset)
        except Exception:
            stop.wait(5)                 # network hiccup: back off, keep running


BOT_COMMANDS = [
    ("start", "Authorize and open the dashboard"), ("help", "List all commands"),
    ("status", "EA connection, mode, account, config version"), ("settings", "Open the settings menus"),
    ("risk", "Show or change risk % per basket"), ("setrisk", "Set risk %, e.g. /setrisk 1"),
    ("target", "Show or change the target multiplier"), ("window", "Show or change the scalp window (minutes)"),
    ("trades", "Show or change baskets per day"), ("mode", "Signal-only or demo mode"),
    ("positions", "Open positions"), ("history", "Recent closed baskets"), ("report", "daily / weekly / monthly report"),
    ("news", "News status and policy"), ("volatility", "Volatility filter state"), ("rules", "Active prop-firm profile"),
    ("importrules", "Import prop-firm rules from text"), ("symbols", "Symbols reported by MT5"),
    ("watchlist", "Symbols the bot may trade"), ("addsymbol", "Add a symbol to the watchlist"),
    ("removesymbol", "Remove a symbol from the watchlist"), ("instrument", "Show one symbol's details"),
    ("scan", "What is being monitored"), ("pause", "Block new entries"), ("resume", "Ask the EA to resume"),
    ("closeposition", "Close a chosen position (confirmed)"), ("shutdown", "Stop new entries and the EA (confirmed)"),
    ("kill", "Kill switch on/off"), ("logs", "Recent events and warnings"), ("account", "Logged-in account (read-only)"),
]
