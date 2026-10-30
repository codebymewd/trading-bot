# Deployment guide (Windows VPS or your own PC)

## 1. What runs where
- **MT5 terminal** (logged in to YOUR trading account by you): runs the EA and indicators. Credentials never leave MT5.
- **Backend** (`backend/run_server.py`): FastAPI + Telegram bot + SQLite. Can run on the same Windows machine or another host.
- **Telegram**: control and alerts only. It never receives broker passwords.

## 2. One-time setup
1. Install Python 3.11+ and run `py -m pip install -r requirements.txt` in the project folder.
2. Create the bot: in Telegram talk to **@BotFather**, `/newbot`, copy the token. Get your numeric user id from **@userinfobot**.
3. Choose a long random shared secret for EA signing (24+ characters). Example in PowerShell:
   `-join ((48..57)+(65..90)+(97..122) | Get-Random -Count 40 | % {[char]$_})`
4. EASIEST: follow `docs/TELEGRAM_SETUP.md` (a private `.env` file plus `tools/telegram_check.py`). Alternatively set environment variables (PowerShell, current user; restart the terminal afterwards):
   ```
   setx TB_EA_SECRET "<your long random secret>"
   setx TB_TELEGRAM_TOKEN "<token from BotFather>"
   setx TB_TELEGRAM_USERS "<your numeric telegram id>"
   setx TB_DB_PATH "C:\TrendBot\trendbot.db"
   ```
   Never put these in source files, screenshots or chat messages. If the token leaks, revoke it with BotFather (`/revoke`).
5. Start it: `py backend\run_server.py` (binds to 127.0.0.1:8000). In Telegram send `/start` to your bot: it answers only your id.
6. Keep it running with Windows Task Scheduler ("At startup", run `py backend\run_server.py`) or NSSM as a service.

## 3. Reaching the backend from MT5
- Same machine: use `http://127.0.0.1:8000`. In MT5: **Tools > Options > Expert Advisors > Allow WebRequest for listed URL**, add that URL.
- Different machine: put an HTTPS reverse proxy (Caddy/Nginx) in front, expose only `/ea/*` and `/health`, add the https URL to the MT5 allow-list. The HMAC signature, timestamp (60 s) and nonce protect the EA endpoints; HTTPS protects the payload.

## 4. EA <-> backend protocol (for the MQL5 EA)
All EA requests are `POST` with a JSON body and three headers:
`X-TB-Timestamp` (unix seconds, +/-60 s), `X-TB-Nonce` (unique per request), `X-TB-Signature` = hex HMAC-SHA256(secret, message) where
`message = METHOD + "\n" + PATH + "\n" + timestamp + "\n" + nonce + "\n" + hex(SHA256(body))`.
MQL5 has no built-in HMAC: implement it with `CryptEncode(CRYPT_HASH_SHA256, ...)` and the ipad/opad construction.
Test vector: secret = 32 times `x`, POST, path `/ea/heartbeat`, timestamp `1700000000`, nonce `abc123`, body `{"a":1}` gives signature
`370f139c739191feba7bff31ddddb2de20508e87d72d03f7bc5c783ee272d632` (checked by `tests/test_docs_vectors.py`).

| Endpoint | EA sends | Backend returns |
|---|---|---|
| `/ea/heartbeat` (every `heartbeat_seconds`) | `account{login,balance,equity,free_margin,is_demo,currency}`, `applied_version`, `state{paused,locked_out}`, `positions[]`, `symbols[{name,asset_class,spec_ok}]`, `gate`, `news`, `volatility` | `server_time`, `stale_config_seconds`, `commands[]` (PAUSE, RESUME, CLOSE_POSITION{ticket}, SHUTDOWN), and `config` (full payload incl. `derived` and `config_hash`) when `applied_version` is behind |
| `/ea/events` | `events[{event_id (unique), type, ...}]` such as BASKET_OPENED, BASKET_CLOSED, SL_MOVED, COMMAND_RESULT{command_id,kind,ok,detail} | `accepted`, `duplicates` (idempotent) |
| `/ea/config/ack` | `version`, `hash`, `ok`, `error` | result text |

Rules for the EA: validate the config itself (same limits as `config/contract.json`); acknowledge only after applying it; if no successful heartbeat for `stale_config_seconds`, stop opening new baskets (broker-side stops stay); never execute a command twice (`command_id`); report every command's result.

## 5. Safe rollout order
1. SIGNAL_ONLY for several days. 2. DEMO account only (put its account number in `broker.allowed_logins` through the config). 3. Review `docs/STATUS.md` checklist. LIVE is disabled in this version.
