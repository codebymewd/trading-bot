"""Start the backend. Configuration comes from environment variables (never from source code):

  TB_EA_SECRET        shared secret used to sign EA requests (long random string)
  TB_DB_PATH          SQLite file (default trendbot.db)
  TB_HOST / TB_PORT   bind address (default 127.0.0.1:8000; put a TLS reverse proxy in front for remote access)
  TB_DASHBOARD_TOKEN  dashboard/API bearer token (recommended; without it the /api stays disabled)
  TB_TELEGRAM_TOKEN / TB_TELEGRAM_USERS  OPTIONAL notifications only - Telegram is NOT required
                      to start, stop, activate, pause, select accounts, change settings or trade.
  (all of these can instead be written in a private .env file in the project folder; see .env.example)
"""
import os
import sys
import threading

import uvicorn

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from backend.envfile import load_env_file  # noqa: E402
from backend.app.service import Service, Settings, create_app  # noqa: E402


def main():
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    load_env_file(os.path.join(root, ".env"))
    secret = os.environ.get("TB_EA_SECRET", "")
    db_path = os.environ.get("TB_DB_PATH", "trendbot.db")
    dash_token = os.environ.get("TB_DASHBOARD_TOKEN", "")
    if len(secret) < 24:
        print("Set TB_EA_SECRET (24+ chars) first.")
        return 2

    notifier = None
    users = {int(x) for x in os.environ.get("TB_TELEGRAM_USERS", "").split(",") if x.strip()}
    token = os.environ.get("TB_TELEGRAM_TOKEN", "")
    if token and users:                     # optional: notifications only, never control-critical
        from backend.app.telegram_client import BOT_COMMANDS, TelegramClient, run_polling  # noqa: E402
        client = TelegramClient(token)
        try:
            client.set_commands(BOT_COMMANDS)
        except Exception:
            print("Could not reach Telegram to set the command menu (check your internet connection / token).")
        notifier = client

    service = Service(Settings(secret, users, db_path, dashboard_token=dash_token), notifier=notifier)
    # legacy global basket store lives next to the database, bound to the default account
    service.store_path = os.path.splitext(db_path)[0] + "_baskets.json" if db_path != ":memory:" else None
    stop = threading.Event()

    if notifier is not None:
        from backend.app.telegram_client import run_polling  # noqa: E402
        threading.Thread(target=run_polling, args=(notifier, service.telegram_update, stop), daemon=True).start()

    def watchdog():
        while not stop.wait(15):
            service.check_ea_connection()
    threading.Thread(target=watchdog, daemon=True).start()
    uvicorn.run(create_app(service), host=os.environ.get("TB_HOST", "127.0.0.1"), port=int(os.environ.get("TB_PORT", "8000")))
    stop.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
