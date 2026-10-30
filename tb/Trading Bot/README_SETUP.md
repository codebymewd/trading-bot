# TrendBot setup (Windows)

1. Extract this folder (e.g. `Desktop\Trading Bot\trendbot`).
2. Open PowerShell inside the folder that contains `pytest.ini`.
3. `py -m pip install -r requirements.txt`   (tzdata is required on Windows for Africa/Lagos)
4. `py -m pytest -q`   -> expect all tests passed.
5. If you change `config/contract.json`: `py tools/gen_mql5.py`, then re-run the tests.

## EA side (not compiled yet - please do this and report results)
1. In MT5: File > Open Data Folder.
2. Copy `ea/Include/*.mqh`  ->  `MQL5\Include\TrendBot\`
3. Copy `ea/Scripts/TestTrendBotCore.mq5` -> `MQL5\Scripts\`
4. Open the script in MetaEditor, press F7 (compile). Send me any compile errors.
5. Run it from Navigator > Scripts on any chart; the Experts tab should print `ALL PASSED`.

---
# Stage A: visual detector (signal-only, no orders)

## Install
1. Copy `ea/Include/TrendBotEngine.mqh` into `MQL5\Include\TrendBot\` (next to the two files already there).
2. Copy `ea/Indicators/TrendBotStageA.mq5` into `MQL5\Indicators\`.
3. MetaEditor (F4): open the indicator, press F7. Send me any errors from the Errors tab.
4. In MT5 drag **TrendBotStageA** from Navigator > Indicators onto an M5 chart (e.g. your Exness gold symbol).
   Algo Trading does NOT need to be on. Open the D1, H4 and H1 charts of that symbol once so MT5 downloads their history.

## What you should see
- Blue/pink dots = confirmed swings (appear 3 bars after the extreme, never earlier)
- BOS (solid) and MSS (gold dashed) lines, yellow dots = displacement
- Green/red boxes = Fair Value Gaps (box ends when the gap is filled/invalid/expired)
- Grey dotted lines = open liquidity levels (PDH/PDL/PWH/PWL, swings, equal highs/lows, sessions)
- Orange arrows = confirmed liquidity sweeps
- Top-left panel: D1 / H4 / H1 / chart trend and the H4+D1 gate

## Parity check (important: proves the MQL5 port matches the tested Python engine)
1. MT5: File > Open Data Folder > `MQL5\Files`. Copy `TrendBot_<SYMBOL>_PERIOD_M5_bars.csv` and `..._events.csv`.
2. In `Trading Bot\trendbot` run:
   `py tools/compare_events.py "<path>\TrendBot_..._bars.csv" "<path>\TrendBot_..._events.csv"`
3. Expect `PARITY OK`. If it prints mismatches, send me the output.

---
# Update for the scalper config (schema v3)
The shared contract changed, so re-copy the EA files (they are regenerated):
1. Copy `ea/Include/TrendBotContract.mqh` and `ea/Include/TrendBotCore.mqh` into `MQL5\Include\TrendBot\` (overwrite).
2. Copy `ea/Scripts/TestTrendBotCore.mq5` into `MQL5\Scripts\` (overwrite), compile with F7 and run it again. Expect `ALL PASSED`.
3. `py -m pytest -q` in the project folder should now show `105 passed`.

---
# Step 2: review the M15 setup / M5 trigger rules on your own data
1. Copy `ea/Scripts/ExportBars.mq5` into `MQL5\Scripts\`, compile (F7), drag it onto a chart of the symbol you want (e.g. your Exness gold pair).
   If it exports fewer bars than requested, set Tools > Options > Charts > "Max bars in chart" to Unlimited and run again.
2. Open `MQL5\Files` (File > Open Data Folder > MQL5 > Files). Copy the four files `TrendBot_<SYMBOL>_PERIOD_D1/H4/M15/M5_bars.csv` into a new folder, e.g. `Trading Bot\data`.
3. In `Trading Bot\trendbot` run: `py tools/run_scalp_reference.py "C:\...\Trading Bot\data" <SYMBOL>`
4. Read the summary, open `scalp_signals_<SYMBOL>.csv`, and check a few CANDIDATE times on the chart: is the zone real, and does the M5 trigger look right?
This is a rules review only; it does not simulate trades or measure profit.

Indicator confirmation is now part of the same run (RSI, MACD, Bollinger, Stochastic, minimum 3 by default). To see its effect:
`py tools/run_scalp_reference.py "<folder>" <SYMBOL> --ind NONE` (structure only) versus the default run, or `--ind RSI,MACD --min 2`.
In the log, each CANDIDATE shows its indicator summary and `indicators_insufficient` rejections show which indicators disagreed.

---
# Step 2b: MQL5 scalp review indicator + parity check
1. Copy these three files into `MQL5\Include\TrendBot\` (overwrite the old TrendBotEngine.mqh):
   `ea\Include\TrendBotEngine.mqh`, `ea\Include\TrendBotIndicators.mqh`, `ea\Include\TrendBotScalp.mqh`
2. Copy `ea\Indicators\TrendBotScalpReview.mq5` into `MQL5\Indicators\`, open it in MetaEditor and press F7. Send me any errors.
3. In MT5 drag **TrendBotScalpReview** onto an M5 chart (Algo Trading can stay off). It loads D1/H4/M15/M5 history (open those charts once first).
   You will see: coloured boxes = M15 setup zones, arrows with entry/stop/target lines = CANDIDATE signals (label shows the indicator result),
   grey x = rejected setups (label = reason). The top-left panel shows the gate and counts. No orders are ever sent.
4. Parity: from `MQL5\Files` copy `TrendBot_<SYMBOL>_PERIOD_D1/H4/M15/M5_bars.csv` and `TrendBot_<SYMBOL>_scalp_signals.csv` to one folder, then run
   `py tools/compare_scalp.py "<folder>" <SYMBOL>` and expect `PARITY OK`.

---
# Step 4: check basket sizing against your broker's real specs
1. Copy `ea\Scripts\ExportSymbolSpec.mq5` into `MQL5\Scripts\`, compile (F7), drag it onto the chart of your symbol. In the settings window enter your account's round-turn commission per 1.0 lot (0 if none; MT5 cannot tell us this).
2. Copy `MQL5\Files\TrendBot_<SYMBOL>_spec.csv` into your `data` folder.
3. Try a plan (numbers only, no orders), e.g. buy at 2000.00 with a stop at 1995.00, 10% basket risk, up to 3 positions:
   `py tools/plan_basket.py "<data folder>\TrendBot_XAUUSDm_spec.csv" --risk-pct 10 --entry 2000 --sl 1995 --dir buy --positions 3`
   Check that the lot sizes, the risk and the target price agree with what MT5 shows for the same trade (e.g. the loss at the stop in the order ticket).

---
# EA phase 1: connect the EA to your Telegram dashboard (no trading code)
1. Copy into `MQL5\Include\TrendBot\`: `TrendBotJson.mqh`, `TrendBotComm.mqh`, `TrendBotRuntime.mqh`, `TrendBotSampleConfig.mqh`
   (from `ea\Include\`; the older .mqh files stay where they are).
2. Copy `ea\Scripts\TestTrendBotComm.mq5` into `MQL5\Scripts\`, compile (F7), run it on any chart. Expert tab must end with `TrendBotComm tests: ALL PASSED`.
   Send me any compile errors or FAIL lines before going on.
3. Copy `ea\Experts\TrendBotEA.mq5` into `MQL5\Experts\` and compile it.
4. Create the file `MQL5\Files\TrendBot_secret.txt` containing ONE line: the same `TB_EA_SECRET` value as in your `.env`.
5. MT5: Tools > Options > Expert Advisors > tick "Allow WebRequest for listed URL" and add `http://127.0.0.1:8000`.
6. Start the backend (`start_backend.bat`).
7. Drag `TrendBotEA` (Navigator > Expert Advisors) onto ANY chart of the account the bot will use. One EA per account.
   Algo Trading can stay OFF: this EA version has no trading functions.
8. The chart panel should say "Backend: connected". In Telegram `/status` should show "EA: connected".
   Then try `/setrisk 0.5`: within about 10 seconds Telegram reports "Configuration version 2 is applied by the EA".
