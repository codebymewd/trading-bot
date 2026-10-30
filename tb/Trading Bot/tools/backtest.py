"""Backtest the scalp strategy on bars exported from MT5 (ExportBars + ExportSymbolSpec scripts).

Usage:
  py tools/backtest.py "<folder>" <SYMBOL> --spread 0.20 [--slippage 0.05] [--equity 1000] [--split 0.7]
                       [--calendar news.json] [--ind NONE] [--min N]
Needs in <folder>: TrendBot_<SYMBOL>_PERIOD_{D1,H4,M15,M5}_bars.csv and TrendBot_<SYMBOL>_spec.csv
--spread / --slippage are in PRICE units (e.g. 0.20 on gold = 20 points). Commission comes from the spec file.
Prints the full-period report and the development / out-of-sample halves. Read the limitations before trusting it.
"""
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from backend.app import basket_manager as bm  # noqa: E402
from backend.app.backtest import (LIMITATIONS, Bars, BtParams, compute_metrics, format_report, simulate,  # noqa: E402
                                  split_trades)
from backend.app.config_schema import SystemConfig  # noqa: E402
from backend.app.news import NewsCalendar  # noqa: E402
from reference.scalp import ScalpEngine  # noqa: E402
from tools.plan_basket import load_spec  # noqa: E402
from tools.run_scalp_reference import load_engine, params_from_config  # noqa: E402


def main(argv):
    if len(argv) < 3:
        print(__doc__)
        return 2

    def opt(name, default=None, cast=float):
        return cast(argv[argv.index(name) + 1]) if name in argv else default

    folder, symbol = argv[1], argv[2]
    cfg = SystemConfig()
    spec, _ = load_spec(os.path.join(folder, f"TrendBot_{symbol}_spec.csv"))
    d1, _ = load_engine(folder, symbol, "D1")
    h4, _ = load_engine(folder, symbol, "H4")
    m15, _ = load_engine(folder, symbol, "M15")
    m5, _ = load_engine(folder, symbol, "M5")
    sp = params_from_config(cfg, opt("--spread", 0.0))
    if "--ind" in argv:
        names = argv[argv.index("--ind") + 1]
        sp.enabled_indicators = () if names == "NONE" else tuple(names.split(","))
        sp.minimum_confirmations = min(sp.minimum_confirmations, len(sp.enabled_indicators))
    if "--min" in argv:
        sp.minimum_confirmations = opt("--min", cast=int)
    signals = ScalpEngine(d1, h4, m15, m5, sp).run()
    cands = [s for s in signals if s.type == "CANDIDATE"]
    cal = None
    if "--calendar" in argv:
        with open(argv[argv.index("--calendar") + 1]) as fh:
            cal = NewsCalendar.from_json(fh.read())
    bt = BtParams(opt("--equity", 1000.0), opt("--spread", 0.0), opt("--slippage", 0.0))
    bars = Bars(m5.tm, m5.o, m5.h, m5.l, m5.c, m5.atr)
    res = simulate(bars, cands, spec, cfg, bm.params_from_config(cfg), bt, cal)
    rej = res.rejected + Counter({"scalp_engine_rejects": sum(1 for s in signals if s.type == "REJECT")})
    print(f"{symbol}: {len(cands)} candidates from the strategy, {len(res.trades)} baskets taken\n")
    print(format_report("FULL PERIOD", compute_metrics(res.trades, bt.start_equity), rej))
    dev, oos = split_trades(res.trades, opt("--split", 0.7))
    print("\n" + format_report("DEVELOPMENT (first part)", compute_metrics(dev, bt.start_equity)))
    print("\n" + format_report("OUT-OF-SAMPLE (last part)", compute_metrics(oos, bt.start_equity)))
    print(f"\nLimitations: {LIMITATIONS}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
