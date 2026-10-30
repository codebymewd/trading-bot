# TrendBot – Architecture & Milestones (Phase 0)

Status: design only. Nothing here has been backtested. No profitability is claimed.

## 1. Component map

```
 Telegram app ──► Telegram Bot API ──► [ FastAPI backend ] ◄──HTTPS (HMAC)──► [ MT5 EA ] ──► Broker
                                         │  SQLite (audit, config,            │  Execution, sizing,
                                         │  profiles, events)                 │  FINAL safety gate,
                                         │  News provider, OCR/parser         │  lockout state, SL/TP
```

| Component | Owns | Must never |
|---|---|---|
| **MQL5 EA** | Market analysis, order placement, position sizing, broker constraints, daily counters, lockout, prop-firm guard (final) | Trust a command it cannot verify; open a trade with stale/unacknowledged config |
| **FastAPI backend** | Telegram auth (allowlist), config validation & versioning, news ingestion, prop-rules parsing, reporting, alert dedupe | Send orders directly; override EA checks |
| **Telegram bot** | UI only (commands, inline keyboards, confirmations) | Hold trading logic; bypass guards |
| **SQLite** | Config versions, audit log, events, profiles, trade ledger mirror | Be the only copy of lockout state (EA keeps its own) |

## 2. EA ⇄ backend protocol

MT5 cannot host an inbound server, so the **EA polls** the backend using `WebRequest()`.
(The backend domain must be whitelisted in MT5: Tools → Options → Expert Advisors.)

- `POST /ea/heartbeat` every N s: EA sends account snapshot, applied `config_version`, state, open-position summary. Response contains pending commands + the latest signed config.
- `POST /ea/events`: trade opened/closed/modified, rejections (with reason code), lockouts, errors. Each event has a unique `event_id` (idempotent insert → no duplicate Telegram alerts).
- `POST /ea/config/ack`: EA confirms `config_version` + `config_hash` applied (or failure reason). **Telegram says "applied" only after this ack.**
- Every request: `X-Timestamp`, `X-Nonce`, `X-Signature = HMAC-SHA256(secret, method|path|ts|nonce|body)`. Backend rejects clock skew > 60 s and replayed nonces. Secret lives in env var on the backend and in an EA input/secure file on the VPS, never in source.
- Commands (pause, close position, etc.) carry `command_id`; EA executes at most once per id and replies with a result event.

### Communication-loss policy (defaults)
- No successful heartbeat for `stale_config_seconds` (default 300) ⇒ EA enters **DEGRADED**: no new entries; existing broker-side SL/TP untouched; break-even/trailing continue using last *applied* config.
- Config with unknown/newer schema, bad hash, or failed validation ⇒ rejected, previous config kept, alert sent.
- On restart: EA rebuilds state from (1) its persisted GlobalVariables/file, (2) MT5 deal history for today, (3) live positions by magic number. Lockout is the **union** of all sources (strictest wins).

## 3. Authority & safety gate order (EA, before every order)

1. Kill switch / mode check (SIGNAL_ONLY → never sends orders)
2. Config fresh & acknowledged
3. Schedule & entry cutoff
4. Instrument validated (state ≥ DEMO/LIVE-eligible for current mode)
5. Trend gate (D1/H4) and setup complete
6. News policy (fail-safe blocks if data stale)
7. Volatility + spread + slippage
8. Portfolio: open risk, correlation, max positions, margin
9. Daily trade count, consecutive losses, daily loss, prop-firm drawdown (+ safety buffer)
10. Stop/target validity vs broker stop-level/freeze-level
11. Volume calc ≤ risk budget; min lot not exceeding budget; margin check (`OrderCalcMargin`)
12. Duplicate-order guard (setup_id + magic + bar time)
13. Send → verify retcode → log. Never report success without confirmation.

Any failure ⇒ **NO TRADE** with a reason code.

## 4. Modes

`SIGNAL_ONLY` (default) → `DEMO` → `LIVE`. LIVE requires: all instruments used are `LIVE_VALIDATED`, active prop profile confirmed, account is not demo-flagged as mismatch, and an explicit two-step Telegram confirmation. EA re-checks `ACCOUNT_TRADE_MODE` itself.

## 5. Instrument lifecycle (spec §23-H)

`DISCOVERED → SPEC_VALIDATED → MONITORED → (SETUP_QUALIFIED) → DEMO_ELIGIBLE → LIVE_VALIDATED`
plus `TEMP_BLOCKED` and `USER_DISABLED` overrides. Unknown/unvalidated ⇒ NO TRADE.
Asset-class parameter sets: forex, metals, crypto, indices, energies, stocks; unsupported class ⇒ monitored only.

## 6. Repository layout

```
trendbot/
  docs/            ARCHITECTURE.md, STRATEGY_SPEC.md, (later) deploy/backtest/demo guides
  config/          example_config.json
  backend/app/     config_schema.py, (later) main.py, auth.py, telegram_bot.py, news.py, rules_parser.py, db.py
  ea/              (later) TrendBot.mq5 + Include/*.mqh  (Structure, Liquidity, FVG, Risk, News, Portfolio, Comms, State)
  tests/           test_config_schema.py, (later) risk sizing, lockout, drawdown, news, config sync
```

## 7. Milestones

| Stage | Deliverable | Exit criterion |
|---|---|---|
| **0 (this)** | Architecture, strategy spec, config schema + tests | You review and approve definitions |
| **A** | MQL5 include files: swings, structure, BOS/MSS, displacement, liquidity map, FVG. Signal-only chart drawing + CSV log | Non-repainting verified (same signals on replay); visual review on XAUUSD/GBPUSD/BTCUSD |
| **B** | Entry state machine + SL/TP/expiry rules | Every setup logs its full step trail |
| **C** | Risk sizing, RR, daily limits, lockout persistence, BE/trailing/partial | Unit-tested sizing across forex/metal/crypto specs |
| **D** | News, volatility, correlation/portfolio filters | Fail-safe behaviour verified with stale data |
| **E** | Prop profile + FastAPI + Telegram + EA comms | End-to-end config ack test |
| **F** | Backtest/forward tests, out-of-sample report | Report with all metrics in spec §20 |
| **G** | Demo-account run per checklist | Zero rule violations over agreed period |

## 8. Known limitations (initial)

- Liquidity pools are price-action proxies; real order books are not observable.
- Strategy Tester cannot replay the live news feed faithfully; news performance needs a stored calendar dataset.
- Correlation is a rolling statistic and can break down abruptly.
- Broker tick data quality and spreads in the tester differ from live.
- Prop-firm rule parsing (OCR) is assistive; the user must confirm every value.

## 9. Stage A status

Implemented: `reference/engine.py` (tested reference), `ea/Include/TrendBotEngine.mqh` (MQL5 port),
`ea/Indicators/TrendBotStageA.mq5` (chart drawing + CSV), `tools/compare_events.py` (parity check).
Deferred to later stages: consolidation-boundary levels, configurable session hours/timezone
(sessions are fixed server-time windows for now), HTF point-of-interest selection (Stage B).
