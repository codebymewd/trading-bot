# TrendBot - project status and handoff

Last test run: 341 Python tests passing. Telegram dashboard setup: docs/TELEGRAM_SETUP.md MQL5 files are compiled/verified by the owner only where noted.

## Built and verified
| Area | Where | Verified how |
|---|---|---|
| Versioned config schema (v3), migration from v2, shared contract | `backend/app/config_schema.py`, `config/contract.json` | Python tests; EA header generated from the contract; EA core test script printed ALL PASSED on the owner's MT5 |
| Lagos-midnight trading day, restart-safe daily counters, lockout | `backend/app/trading_day.py`, `ea/Include/TrendBotCore.mqh` | Python tests + shared test vectors |
| Stage A detection (swings, BOS/MSS, displacement, FVG, liquidity, sweeps) | `reference/engine.py`, `ea/Include/TrendBotEngine.mqh`, `ea/Indicators/TrendBotStageA.mq5` | Python tests incl. no-repaint property tests; parity tool |
| M15 setup / M5 trigger / D1+H4 gate | `reference/scalp.py`, `ea/Include/TrendBotScalp.mqh`, `ea/Indicators/TrendBotScalpReview.mq5` | Python tests; owner reported parity OK on real data |
| Indicator confirmation (RSI, MACD, Bollinger, Stochastic, CCI) | `reference/indicators.py`, `ea/Include/TrendBotIndicators.mqh` | Python tests; same parity run |
| Broker-aware basket sizing, basket target/max loss, timeout, break-even | `backend/app/sizing.py`, `backend/app/basket_manager.py` | Python tests across several symbol specs; spec export script for real values |
| Safety gate, execution, recovery, prop-firm guard, account allowlist | `backend/app/executor.py`, `backend/app/propguard.py` | Python tests against a simulated broker |
| News engine (impact policies, windows, fail-safe, prop news rules, anomaly guard) | `backend/app/news.py` | Python tests |
| Portfolio filters (shared currency exposure, rolling correlation) and asset-class volatility | `backend/app/portfolio.py`, `backend/app/instruments.py` | Python tests |
| Instrument validation lifecycle, asset-class defaults, setup ranking | `backend/app/instruments.py` | Python tests |
| Backtester with the live sizing/basket/daily-limit logic and the full metrics report | `backend/app/backtest.py`, `tools/backtest.py` | Python tests with exact-money scenarios |
| Backend service: signed EA endpoints, config versioning with EA acknowledgement, audit log, alerts with de-duplication | `backend/app/service.py`, `protocol.py`, `settings.py`, `db.py` | FastAPI TestClient tests |
| Telegram controller (allow-list, settings menus, confirmations, commands, watchlist, reports, rule import) | `backend/app/telegram_ctl.py`, `telegram_client.py`, `prop_rules.py` | Controller tests; Bot API client tested against a mock transport (never run against the real Telegram yet) |

## NOT built yet (the bot still cannot trade, even on demo)
1. **MQL5 EA, remaining phases.** Phase 1 (signed backend connection, config validation/ack, pause/resume/shutdown, events; no trading code) is written but not yet compiled by anyone. Still to build: phase 2 (signal detection inside the EA with 'would trade' events using the sizing/gate port) and phase 3 (demo-only order execution, basket management, state persistence/recovery). No order-sending MQL5 code exists. These can only be verified by compiling and testing in MetaEditor on your side.
2. **Calendar feed inside the EA** (MT5 economic calendar -> news status in the heartbeat). The backend news logic exists and is tested; the EA side does not.
3. **Real-world verification:** the Telegram bot and backend have not been run against real Telegram or a real EA; backtests and demo runs have not been done, so no performance has been measured or is claimed.
4. OCR/PDF import needs optional libraries (see requirements.txt); text import works now.
5. Instrument scanning is logic only (ranking/selection); the EA does not feed it yet.

## Known limitations
- Liquidity pools are price-action proxies, not observed order books.
- Quasimodo and key-level-rejection setups are not implemented (only FVG and sweep zones).
- Sessions are fixed server-time windows; consolidation boundaries are not tracked.
- Prop guard does not yet enforce overnight/weekend, consistency, minimum-days or news-window rules.
- Indicator math is the project's own reference implementation; MT5 built-ins differ slightly during warm-up.
- Break-even and other management rules are tested logic, not yet validated on live price data.
- Strategy Tester cannot replay the live news feed faithfully.

## Account access (important)
The bot never receives your broker password. You log in to the trading account yourself in the MT5 terminal
(File > Login to Trade Account). The EA only trades if the logged-in account number is in `broker.allowed_logins`
(max 5 numbers, no passwords), the mode is DEMO on a demo account, and every other gate check passes.
Telegram will be a control and reporting channel only (status, settings, pause, close, reports); it will show the
current account number and balance read-only.

## Before any real money (checklist)
1. EA compiled with 0 errors; unit/parity checks pass on your own broker data.
2. Run SIGNAL_ONLY for at least several days; review every would-trade line against the chart.
3. Demo account only (allowed_logins set to the demo account); confirm stops/targets attached to every order, basket close at target, timeout behaviour, restart recovery (stop MT5 mid-basket and restart).
4. Check broker minimum lots against your account size (a $100 account cannot split risk across several positions on most symbols).
5. Review the daily loss limit, lockout and prop-profile settings with the Telegram confirmation flow.
6. Only then consider LIVE, which is intentionally unavailable in this version and requires an explicit code and configuration change after a documented demo period.

## Trading mode: DEMO vs LIVE (v0.1)

The system has three modes (`mode` in the config, changeable from Dashboard → Settings → Risk):

| Mode | Behaviour |
|------|-----------|
| `SIGNAL_ONLY` (default) | Full safety gate runs; "would trade" reported; **no orders ever sent** |
| `DEMO` | Real market orders via the EA — only on a demo MT5 account whose login is in `broker.allowed_logins` and matches the account's `mt5_login` |
| `LIVE` | **Hard-blocked.** `executor.LIVE_TRADING_AVAILABLE = False`. The settings UI does not even offer it as a choice. |

### How to switch SIGNAL_ONLY ↔ DEMO
Dashboard → Settings → Risk → "Trading mode" → choose `DEMO` → confirm twice (always_confirm). The change goes through the normal propose → new version/hash → EA ack flow; it shows "Applied" only after the EA acknowledges.

### Unlocking LIVE (deliberate multi-step procedure — do NOT rush this)
1. Complete a documented DEMO forward test (minimum 4 weeks) with clean prop-drawdown behaviour.
2. Set every instrument you intend to trade to state `LIVE_VALIDATED` in the config.
3. In the config JSON set `"mode": "LIVE"` **and** `"live_confirmed": true`. Pydantic validation refuses LIVE otherwise.
4. Only then flip `LIVE_TRADING_AVAILABLE = True` in `backend/app/executor.py` and restart the backend.
5. The EA independently re-checks `ACCOUNT_TRADE_MODE` broker-side and will refuse to send live orders from a demo terminal (and vice-versa: DEMO mode refuses live accounts — reason code `demo_mode_on_live_account`).

Until step 4 is done, any attempt to run LIVE is rejected at the gate with the exact reason `live_trading_not_available`, recorded in the activity feed and audit log.
