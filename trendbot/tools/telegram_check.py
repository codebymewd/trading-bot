"""Check your Telegram setup BEFORE starting the backend.

Usage (PowerShell, from the project folder):   py tools/telegram_check.py
Reads TB_TELEGRAM_TOKEN and TB_TELEGRAM_USERS from the environment or from the .env file.
It verifies the token, then sends a test message to each allowed user id. It never prints the token.
"""
import os
import sys

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from backend.envfile import load_env_file  # noqa: E402
from backend.app.telegram_client import TelegramClient  # noqa: E402


def check(token: str, users, http=None, out=print) -> bool:
    if not token:
        out("TB_TELEGRAM_TOKEN is not set. Put it in the .env file (see .env.example).")
        return False
    if not users:
        out("TB_TELEGRAM_USERS is not set. Put your numeric Telegram id in the .env file.")
        return False
    client = TelegramClient(token, http)
    try:
        me = client.get_me()
    except Exception as e:
        out(f"Could not reach Telegram ({type(e).__name__}). Check your internet connection.")
        return False
    if not me.get("ok"):
        out("Telegram rejected the token (401). Copy it again from @BotFather, with no spaces or quotes.")
        return False
    out(f"Token OK. Your bot is @{me['result'].get('username')}.")
    good = True
    for uid in users:
        r = client.http.post(client._url + "/sendMessage", json={"chat_id": uid, "text": "TrendBot dashboard check: this chat is connected."}).json()
        if r.get("ok"):
            out(f"Test message delivered to {uid}.")
        else:
            good = False
            out(f"Could not message {uid}: {r.get('description')}. Open your bot in Telegram and press START first, "
                "and make sure the id is YOUR numeric id (from @userinfobot), not a username.")
    return good


if __name__ == "__main__":
    load_env_file(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"))
    ids = [int(x) for x in os.environ.get("TB_TELEGRAM_USERS", "").split(",") if x.strip().isdigit()]
    sys.exit(0 if check(os.environ.get("TB_TELEGRAM_TOKEN", ""), ids) else 1)
