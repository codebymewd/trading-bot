# Backtesting and validation instructions

1. Export data from MT5 for your symbol: run `ExportBars.mq5` (bars) and `ExportSymbolSpec.mq5` (specs, with your commission per lot) into `MQL5\Files`; copy the CSVs to one folder. Set Tools > Options > Charts > Max bars in chart to Unlimited for long history.
2. Run: `py tools/backtest.py "<folder>" <SYMBOL> --spread 0.20 --slippage 0.05 --equity 1000`
   (spread/slippage are in price units; use your broker's typical and bad-case values, and run both.)
3. Optional news simulation: `--calendar news.json` with `{"fetched_at":..., "covers_until":..., "events":[{"time","currency","impact","title","kind"}]}`.
4. Read the three blocks: FULL PERIOD, DEVELOPMENT, OUT-OF-SAMPLE. Decide parameters using the development part only. If the out-of-sample part disagrees badly, the rules are probably overfitted.
5. Metrics reported: trades, win rate, profit factor, net return, max drawdown, average win/loss, expectancy, consecutive wins/losses, per instrument, per session, per volatility level, near high-impact news, exit reasons, and rule rejections (spread, chase, lockouts, min-lot, news).
6. Limitations (also printed): bar data only (stop assumed before target inside one bar), constant spread/slippage, no prop-guard/correlation simulation, news only with a calendar. A backtest is not a forecast.
7. Forward test: run SIGNAL_ONLY and compare "would trade" lines with the chart for several days; then a demo-only run of at least 14 days with zero rule violations before any instrument can be promoted (see `backend/app/instruments.py`).
Test different regimes: trending, ranging, high volatility and news periods; test each instrument separately; never optimise only for maximum profit.
