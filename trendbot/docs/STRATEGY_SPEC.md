# TrendBot – Strategy Definitions (v0.1, for review)

All parameters are configurable (see `config/example_config.json`). Defaults are starting points, **not** optimized values. All logic uses **closed bars only** (index ≥ 1); the forming bar is never used.

## 1. Swings (no look-ahead)

A swing high at bar `i` is confirmed only when `High[i]` is strictly greater than the highs of the `L` bars to its left and the `R` bars to its right. It becomes *known* at bar `i+R` (its `confirm_time`). Signals may only reference swings whose `confirm_time ≤ decision_time`.
Defaults: `L = R = 3` on M5/M15, `2` on H1, `2` on H4/D1. Swing lows symmetric.
Minimum swing size: `|swing − adjacent opposite swing| ≥ min_swing_atr × ATR(14)` (default 0.5) to filter noise.

## 2. Structure

- **External structure**: swings on H4 (and D1 for bias). **Internal structure**: swings on H1/M15/M5 *inside* the current H4 leg.
- **Bullish trend (per timeframe)**: last two confirmed swing highs ascending AND last two confirmed swing lows ascending, and no unrecovered bearish MSS since.
- **Bearish trend**: mirror. Otherwise **RANGE/UNCLEAR**.
- **BOS (bullish)**: a bar **closes** above the most recent confirmed swing high (wick-only does not count; `bos_close_buffer_atr` default 0.05).
- **MSS/CHOCH**: in an up-trend, a close below the most recent confirmed *higher low* that produced the last BOS. This marks the trend invalid for continuation entries until a new HH/HL sequence forms.
- **Displacement**: a candle (or up to `disp_max_bars` = 3 consecutive same-direction candles) with body ≥ `disp_body_atr` × ATR (default 1.2) and body ≥ `disp_body_ratio` of range (default 0.6), that also creates an FVG.
- **Impulse vs. correction**: a leg is *impulsive* if it contains displacement and breaks structure; otherwise corrective.
- **Premium/Discount**: dealing range = last confirmed external swing low→high (bull). Equilibrium = 50 %. Longs must be in discount (< 50 %, configurable up to `max_entry_range_pct`, default 50); shorts in premium.

## 3. HTF bias gate (trend-following only)

Trade direction = D1 and H4 agreement. Rule (default `htf_mode = "H4_AND_D1"`): H4 trend must equal D1 trend. Option `"H4_ONLY"` available for testing. Conflict, RANGE, or missing data ⇒ **NO TRADE**.

## 4. Liquidity map

Levels tracked with type, price, timeframe, `created_time`, `swept` flag:
PDH/PDL, PWH/PWL, confirmed swing H/L (H1/H4), equal highs/lows, session H/L (Asian/London/NY, configurable in server time), consolidation bounds.
- **Equal highs/lows**: ≥ 2 swings within `eq_tolerance_atr` × ATR (default 0.1) of each other.
- **Sweep (bearish-side sweep of lows in an up-trend)**: bar trades below level by ≥ `sweep_min_pierce_atr` (0.05) **and** closes back above the level within `sweep_confirm_bars` (default 2). Sweeps of highs in a down-trend mirror.
- A sweep alone never triggers a trade. In an up-trend a sell-side sweep (of lows) is *consistent*; a buy-side sweep followed by acceptance above is treated as a BOS, not a sweep.
- **Trap/false-breakout filter**: reject if after a break of an external level, price closes back inside within `trap_bars` and displacement is against the trade direction. Also reject failed-continuation (BOS followed by close back below the broken level within `failed_cont_bars`).

## 5. Fair Value Gap

Bullish FVG on closed candles `c1,c2,c3` (c3 most recent): `Low[c3] > High[c1]`; gap = `[High[c1], Low[c3]]`, size = `Low[c3] − High[c1]`.
Bearish: `High[c3] < Low[c1]`; gap = `[High[c3], Low[c1]]`.
Qualify if: size ≥ `fvg_min_atr` × ATR (default 0.2) AND ≥ `fvg_min_points`; c2 is displacement; formed by a move that caused a BOS; direction = HTF bias; lies in/near the HTF POI; age ≤ `fvg_max_age_bars`; not invalidated.
- **Mitigation**: `touch` (any wick enters), `half` (price reaches 50 %), `full` (closes through). Default: gap becomes *used* after the first entry attempt or fully mitigated at `full`.
- **Invalid**: close beyond the far side of the gap against direction.

## 6. Higher-timeframe point of interest (POI)

Default POI = the H1 origin zone of the leg that broke structure (last opposite candle body range before displacement) *or* an H4 FVG, located in discount/premium per §2. POI must be unmitigated and within the H4 trend leg.

## 7. Entry state machine (per symbol & direction)

```
IDLE → BIAS_OK → POI_FOUND → [LIQ_INTERACTION if required] → DISPLACEMENT → BOS_EXEC_TF → FVG_VALID → WAIT_RETRACE → RECHECK → ORDER_READY → (ORDER_SENT) → DONE
              ↘ any failure / invalidation / expiry ⇒ NO_TRADE(reason)
```
- **Entry price**: limit at FVG consequent encroachment (50 %) by default; options `edge` or `market_on_touch_with_confirmation`.
- **Stop-loss**: beyond the swing that originated the displacement leg (swing low for longs) minus `sl_buffer_atr` (default 0.1) × ATR and minus current spread. This is also the invalidation level: close beyond it cancels the setup.
- **Setup expiry**: `setup_max_bars` (default 24 exec-TF bars) after FVG creation; pending order expires with it.
- **Chase filter**: if price is already > `max_chase_r` (default 0.5 R) beyond planned entry in the profit direction, skip.
- **Minimum stop**: ≥ `min_sl_atr` × ATR and ≥ broker stop-level + spread; maximum stop ≤ `max_sl_atr` × ATR (reject oversized).
- **Target**: `entry ± RR × |entry − SL|`, RR ∈ [2, 5], default 3. Rejected if beyond the next opposing HTF external level by more than `tp_beyond_htf_tolerance` (configurable; default: warn only).

## 8. Management (Stage C)

Break-even methods **A** (R multiple, default 1.0), **B** (R multiple + optional partial %), **C** (structural protection: move to BE after a confirmed new internal HL/LH beyond +1R). Optional trailing: behind confirmed swings with ATR buffer. Stops only tighten; any modify failure ⇒ log + alert, state remains "not moved".

## 9. Loss counting

Trade outcome = Σ(profit + commission + swap + fees) over all deals of the position (identified by `POSITION_ID`). Net < 0 ⇒ loss. Partial-close + stop-out with net ≥ 0 is **not** a loss. Manual closes count by the same net rule. Break-even exits with |net| ≤ `be_tolerance` (default small cost threshold) count as neutral, not loss. Two losses in a trading day ⇒ lockout until daily reset; persisted.

## 10. Decisions (confirmed)

1. HTF gate: `H4_AND_D1` (both must agree).
2. Entry: SUPERSEDED by docs/SCALPER_SPEC.md (market order after M5 confirmation; was `fvg_50` limit).
3. Sweep: optional (`sweep_required = false`), contributes to setup score.
4. Trading day: `Africa/Lagos` (WAT, UTC+1), reset `00:00`; rolls at 23:00 UTC. Schedule timezone also `Africa/Lagos` so the 00:00-21:00 window and the day boundary agree.
5. Broker: Exness. Symbols and specifications are discovered from the connected terminal; no hardcoded names or suffixes.
6. Mode: `SIGNAL_ONLY` until explicit user confirmation and validation.
