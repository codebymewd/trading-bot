"""Start the backend. Configuration comes from environment variables (never from source code):

  TB_EA_SECRET       shared secret used to sign EA requests (long random string)
  TB_TELEGRAM_TOKEN  bot token from @BotFather
  TB_TELEGRAM_USERS  comma-separated numeric Telegram user ids allowed to control the bot
  TB_DB_PATH         SQLite file (default trendbot.db)
  (all of these can instead be written in a private .env file in the project folder; see .env.example)
  TB_HOST / TB_PORT  bind address (default 127.0.0.1:8000; put a TLS reverse proxy in front for remote access)
"""
import os
import sys
import threading

import uvicorn

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from backend.envfile import load_env_file  # noqa: E402
from backend.app.service import Service, Settings, create_app  # noqa: E402
from backend.app.telegram_client import BOT_COMMANDS, TelegramClient, run_polling  # noqa: E402


def main():
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    load_env_file(os.path.join(root, ".env"))
    secret = os.environ.get("TB_EA_SECRET", "")
    token = os.environ.get("TB_TELEGRAM_TOKEN", "")
    users = {int(x) for x in os.environ.get("TB_TELEGRAM_USERS", "").split(",") if x.strip()}
    if len(secret) < 24 or not token or not users:
        print("Set TB_EA_SECRET (24+ chars), TB_TELEGRAM_TOKEN and TB_TELEGRAM_USERS first.")
        return 2
    client = TelegramClient(token)
    try:
        client.set_commands(BOT_COMMANDS)          # fills the command menu in Telegram automatically
    except Exception:
        print("Could not reach Telegram to set the command menu (check your internet connection / token).")
    service = Service(Settings(secret, users, os.environ.get("TB_DB_PATH", "trendbot.db")), notifier=client)
    stop = threading.Event()
    threading.Thread(target=run_polling, args=(client, service.telegram_update, stop), daemon=True).start()

    def watchdog():
        while not stop.wait(15):
            service.check_ea_connection()
    threading.Thread(target=watchdog, daemon=True).start()
    uvicorn.run(create_app(service), host=os.environ.get("TB_HOST", "127.0.0.1"), port=int(os.environ.get("TB_PORT", "8000")))
    stop.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
