# Create your Telegram dashboard (about 10 minutes)

## A. Create the bot
1. In Telegram search for **@BotFather** (blue tick) and press START.
2. Send `/newbot`. Give it a display name (e.g. `My TrendBot`) and a username ending in `bot` (e.g. `my_trend_xyz_bot`).
3. BotFather replies with a **token** (looks like `123456789:AAH...`). Treat it like a password. Never paste it in chat, screenshots or code.
4. Optional but recommended: send `/setjoingroups` -> choose your bot -> **Disable** (nobody can add it to groups).

## B. Find your numeric Telegram id
1. Search for **@userinfobot** and press START. It replies with `Id: 123456789`. That number (not your @username) is what the bot allows.

## C. Put the secrets in a private file
1. Open the project folder (the one with `pytest.ini`). Copy `.env.example` to a new file named exactly `.env`
   (in File Explorer: copy, paste, rename; enable "File name extensions" in the View menu if Windows hides them).
2. Open `.env` in Notepad and fill in:
   ```
   TB_TELEGRAM_TOKEN=123456789:AAH...your token...
   TB_TELEGRAM_USERS=123456789
   TB_EA_SECRET=any long random text, 40+ characters
   TB_DB_PATH=trendbot.db
   ```
   `TB_EA_SECRET` is shared later with the EA; make it long and random (mash the keyboard, or use a password generator).
3. Never share `.env`. It is already listed in `.gitignore`.

## D. Install and check
1. Open PowerShell in the project folder (type `powershell` in the folder's address bar).
2. `py -m pip install -r requirements.txt`
3. Open your bot in Telegram (search its username) and press **START** once (Telegram bots cannot message you before this).
4. `py tools/telegram_check.py`
   Expected: `Token OK. Your bot is @...` and `Test message delivered to ...`, and a message appears in Telegram.
   If it says "rejected the token": re-copy the token. If it says "press START": do step 3 first.

## E. Start the dashboard
1. Double-click `start_backend.bat` (or run `py backend\run_server.py`). Leave the window open.
   The bot's command menu (the `/` button in Telegram) fills in by itself.
2. In Telegram send `/start`, then `/settings`, `/status`.
   Before the EA connects, `/status` says "never connected": that is normal. You can already change settings: try `/setrisk 0.5`.
   It will say "Saved ... Waiting for the EA to confirm": settings count as applied only after the EA acknowledges them.
3. Anyone else who messages the bot gets no answer, and the attempt is written to the audit log.

## F. What the dashboard can do now
- Settings menus: risk %, target multiplier, scalp window, positions per basket, indicators on/off, minimum confirmations, break-even (method, trigger, offset), timeout action, news policy, volatility states, baskets per day, daily loss limit, mode (signal-only/demo), trend gate, active prop profile.
- Risky changes show the old and new value and wait for your Confirm button. Risk above the prop-firm limits is refused.
- `/importrules` for prop-firm rules, `/watchlist` `/addsymbol` `/removesymbol`, `/report`, `/history`, `/logs`, `/account`.
- `/pause`, `/resume`, `/closeposition`, `/shutdown` are queued for the EA and reported only when the EA confirms them.
- It never asks for your broker password.

## G. Keep it running
- Task Scheduler: create a task "At startup" that runs `start_backend.bat`, or run it on your VPS the same way.
- If you ever leak the token: BotFather `/revoke`, then update `.env` and restart.
