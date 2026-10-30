# Market-execution scalper (schema v3) - what changed and what is next

One system: the Stage A engine and config stay; pending-order entry and the fixed per-trade RR are replaced.

## Flow
D1+H4 agree (`H4_AND_D1`) -> M15 setup/location -> M5 price-action confirmation -> indicator confirmations -> risk/news/volatility/spread checks -> MARKET order(s) in one basket.
Any failed mandatory step = NO TRADE with a reason code. Default mode is still `SIGNAL_ONLY`.

## Config map (all validated by backend `config_schema.py` and by the EA via the shared `contract.json`)
| Setting | Where | Default |
|---|---|---|
| Risk % (whole basket) | `risk.risk_percent` | 1.0 |
| Max aggregate open risk | `risk.max_open_risk_percent` | 1.0 |
| Max simultaneous positions (global) | `risk.max_simultaneous_positions` | 1 |
| Positions per basket | `scalp.max_positions_per_basket` (<= global) | 1 |
| Target multiplier (x basket risk) | `scalp.target_multiplier` 0.5-10, presets 1-5 | 3.0 |
| Scalp window (minutes) | `scalp.scalp_window_minutes` 1-240, presets 5/15/30 | 15 |
| Timeout action | `scalp.timeout_action` | CLOSE_ALL |
| Basket max loss | `scalp.basket_max_loss_percent` (<= basket risk) | = basket risk |
| Max spread / stop distance | `scalp.max_spread_to_stop_ratio` | 0.15 |
| Break-even | `break_even.method` off/A/B/C, `trigger_r`, `be_offset_points` | off |
| Indicators / minimum | `confirmation.enabled_indicators`, `minimum_confirmations` | RSI, MACD, BB, STOCH / 3 |
| Timeframes | `structure.setup_tf` M15, `structure.execution_tf` M5 | M15 / M5 |
| Daily limit unit | `daily.count_unit` (baskets) | basket |

Basket budget example (tested): equity $100, risk 10%, multiplier 3x -> risk $10, target $30. Three positions share the $10; they never get $10 each.

## Indicator roles (Step 3, implemented in `reference/indicators.py`)
Price action/structure is mandatory and decided first. Indicators only add confirmation; they are not independent votes.
- RSI: momentum regime around the midline (default 50), not 30/70 reversals.
- MACD: zero-line side, histogram expansion/contraction, momentum transition.
- Bollinger: squeeze/expansion and middle-band behaviour, not band-touch reversals.
- Stochastic: pullback losing momentum and turning with the HTF trend, not overbought/oversold reversals.
- CCI: optional acceleration (zero line / threshold), off by default.
`minimum_confirmations` must not exceed the enabled count (rejected otherwise).

Exact rules (direction d = +1 long, -1 short; evaluated on the closed M5 trigger bar):
- RSI (Wilder): CONFIRM if (RSI - midline) x d > 0; NEUTRAL within 5 points below the midline; else CONFLICT.
- MACD: CONFIRM if the histogram moves in direction d AND (MACD line or histogram is on the d side of zero); CONFLICT if the histogram moves against d while on the wrong side of zero; else NEUTRAL.
- Bollinger: CONFIRM if close is on the d side of the middle band AND bandwidth is expanding AND not in a squeeze (bandwidth > 75% of its recent mean); CONFLICT if on the wrong side and expanding.
- Stochastic (slow): CONFIRM if %K is on the d side of %D, moving in d, and crossed within the last 3 bars; CONFLICT if on the wrong side and moving against d.
- CCI (optional): CONFIRM if CCI is on the d side of zero and moving in d; CONFLICT if on the wrong side and moving against.
Only CONFIRM counts. Indicators run AFTER the structure checks (stop size, spread, chase, premium/discount), so they can never rescue an invalid setup; a valid setup with too few confirmations is rejected as `indicators_insufficient` with the per-indicator result logged (e.g. `RSI+ MACD0 BB+ STOCH+ (3/3)`).
Indicator math is a Python reference (Wilder RSI, EMA-based MACD seeded with the first price, population-std Bollinger, slow Stochastic, standard CCI). MT5's built-in indicators differ slightly during warm-up; the MQL5 port will be compared against this reference.

## Superseded earlier decisions
- Limit order at the FVG midpoint (`fvg_50`) -> market order after M5 confirmation. Old values are rejected; `migrate_config()` upgrades stored v2 configs.
- Fixed 1:3 RR per trade -> basket profit target (multiplier x basket risk). The `rr` section is removed.
- Break-even default changed from on (1R) to OFF, per the new rule not to move stops aggressively unless configured.

## Assumptions I made (change any of them)
Default window 15 min, default timeout CLOSE_ALL, target multiplier custom range 0.5-10, positions per basket max 10, daily limits count baskets, Telegram presets are UI shortcuts only (server validates every value).

## Step 2 rules (implemented in `reference/scalp.py`, signal-only)
Per closed M5 bar, using only bars closed at that moment (an M15 event is usable only after its M15 bar closes):
1. Gate: D1 and H4 trends must agree (`H4_AND_D1`); otherwise no zone may trigger.
2. M15 zones (reuse of the Stage A engine events):
   - FVG zone: an M15 FVG linked to a BOS (configurable), aligned with the gate. Ends when filled, invalid, expired (zone age) or when M5 closes beyond its far side.
   - Sweep zone: a liquidity level swept on M15 (lows for longs, highs for shorts). Needs the M5 close to reclaim beyond the swept level.
3. A zone becomes armed when an M5 bar trades into it (sweep zones arm immediately). A zone never acts on the bar where it became known.
4. Trigger (M5 price action): within `trigger_window_bars` after arming, a bar in the gate direction that also produces an M5 BOS or displacement in that direction.
5. Checks: stop from the zone's invalidation +/- buffer (min/max in M15 ATR), spread vs stop, no late chase beyond `max_chase_r` from the zone edge, premium/discount of the M15 dealing range when one exists.
6. Output CANDIDATE (entry reference = M5 close, stop, reference target = entry +/- multiplier x stop distance, which is where an equal-entry basket reaches its target) or REJECT with a reason code. Each zone yields at most one candidate/reject.
Not yet: indicator confirmation (step 3), risk/basket sizing (step 4), news/volatility filters, Quasimodo and key-level-rejection setups (not in this codebase yet). `tools/run_scalp_reference.py` reviews these signals on exported MT5 data; it does not simulate trades.

## Step 5a rules (execution reference: `executor.py`, `propguard.py`; tested against a simulated broker)
Gate, in this order (first failure wins, always with a reason code): kill switch -> stale config (> `safety.stale_config_seconds`) -> trading window (Lagos time, entry cutoff) -> instrument validated for the mode -> symbol spec valid and trading allowed -> stale signal (> 20 s after the M5 bar closed) -> duplicate setup -> news (restricted, or unavailable while `news.on_data_unavailable = block`) -> volatility state allowed -> live quote -> price moved against us since the signal (> `max_chase_r`) -> live spread vs stop -> own daily loss limit -> basket sizing with the LIVE entry price -> exact broker margin -> exposure (lockout, daily basket limit, one basket per symbol, position cap, aggregate risk) -> prop-firm guard -> permission by mode. Before everything else (except the kill switch) the logged-in account number must be in `broker.allowed_logins` for DEMO/LIVE.
Modes: SIGNAL_ONLY runs the whole gate and reports "would trade" but never calls the broker. DEMO sends orders only on a demo account. LIVE is not available in this version (`LIVE_TRADING_AVAILABLE = False`).
Execution: the setup is recorded as used BEFORE the first order (no duplicates after retries, reconnects or restarts); every order carries its stop and the basket target price; a rejected first order opens nothing and is never retried; a rejected later order keeps the smaller basket with risk/target recomputed from the real fills; if slippage pushes actual risk more than 10% over the budget, everything is closed immediately.
Management and recovery: broker-side stop/target closes are detected and booked; close/modify failures are logged and retried, never reported as done; after a restart the saved baskets are reconciled with the broker's positions (net results recorded, daily counters updated); positions the bot does not recognise are reported and never managed or adopted; an unreadable state file locks trading.
Prop guard: confirmed profile required; restricted symbols; max lot per position; daily loss and max drawdown (static/trailing, balance/equity basis) including the new basket's worst-case loss and the safety buffer. Assumption to confirm per firm: daily loss is measured against the starting balance unless `daily_loss_reference` says day-start balance. Not enforced yet: overnight/weekend, consistency, minimum-days and news-window rules.
Until the news engine exists (step 6) the news status is 'unavailable', which BLOCKS trading by default. For demo testing before then you must explicitly set `news.on_data_unavailable = "allow"`.

## Remaining steps
2b. DONE (needs your compile + parity run): MQL5 port `TrendBotScalp.mqh` + `TrendBotIndicators.mqh`, review indicator `TrendBotScalpReview.mq5`, parity tool `tools/compare_scalp.py`.
3. DONE (Python reference + tests): indicator confirmation engine.
4. DONE (Python reference + tests): `backend/app/sizing.py`, `backend/app/basket_manager.py`, basket accounting in `trading_day.py`. See 'Step 4 rules' below.
5a. DONE (Python reference + simulated-broker tests): gate, execution, management, recovery, prop guard.
5b. MQL5 EA port of sizing, gate, basket manager, execution and persistence; signal-only first, then demo-only; parity tests against the reference.
6. Telegram controls, news and volatility engines.


## Step 4 rules (basket sizing and management, reference implementation)
Sizing (`sizing.py`)
- Loss per lot at the stop = ticks x the symbol's own loss tick value + commission per lot. Volumes round DOWN to the broker step, so planned risk can only be at or under the budget. If the minimum lot would risk more than the budget the basket is rejected (`min_lot_exceeds_risk`); risk is never raised silently.
- The basket opens as many positions as `max_positions_per_basket` allows (the 4%/3%/3% idea: `risk_split` equal or front-loaded), using fewer when the minimum lot makes the split infeasible. All positions share ONE budget; planned risk is the sum, never a multiple.
- Rejections: invalid/missing symbol specs, trading not allowed, stop on the wrong side, stop inside the broker stop level, insufficient margin, per-position lot cap below the minimum volume.
- Target price is where the basket's NET profit (after commission) equals planned risk x multiplier, rounded in our favour. The target is based on the planned (post-rounding) risk.
Management (`basket_manager.py`; evaluation is pure, state changes only through `confirm_*` after the broker accepts)
- Close all at basket target, or at basket max loss (default = basket risk; can be set lower).
- Timeout actions: CLOSE_ALL; CLOSE_IF_LOSS (close if net < 0, else keep running); KEEP_RUNNING; MOVE_TO_BREAKEVEN (only if the break-even stop is valid right now: in profit and beyond the broker stop level; otherwise keep the original stops and mark the timeout handled).
- Break-even (default OFF): A = move to average entry +/- offset after the trigger R multiple; B = same plus partial close (positions at minimum size are not split); C = move to a supplied structural protection price (never looser, once). Stops only tighten and must respect the broker stop level; a failed modification leaves the state unchanged and is retried, never reported as done.
- No additions to a basket after opening (no averaging, no martingale). One open basket per symbol. Aggregate open risk (current stops) and the position cap are checked before opening; baskets whose stop is at break-even carry zero risk.
- Daily counters count baskets as trades. A basket's net result (costs included) is WIN, LOSS or NEUTRAL (within the tolerance); two losses, the consecutive-loss limit or the daily loss limit lock out new baskets until the Lagos-midnight reset.
Not yet: EA order execution and state recovery after a restart (step 5), Telegram controls and news/volatility engines (step 6), prop-firm guard in the order path.
