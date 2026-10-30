"""Run the Step 2 setup/trigger engine on bars exported from MT5 (ExportBars script).

Usage:  py tools/run_scalp_reference.py <folder-with-csv-files> <SYMBOL> [--spread 0.0] [--ind RSI,MACD|NONE] [--min N]
Needs, in the folder:  TrendBot_<SYMBOL>_PERIOD_D1_bars.csv, ..._H4_..., ..._M15_..., ..._M5_...
Writes scalp_signals_<SYMBOL>.csv next to the files and prints a summary.
SIGNAL REVIEW ONLY: this reports where the rules would have flagged setups. It does not
simulate trades and says nothing about profitability.
"""
import csv
import os
import sys
from collections import Counter
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from backend.app.config_schema import SystemConfig  # noqa: E402
from reference.engine import Engine, Params, gate, htf_trend_at  # noqa: E402
from reference.indicators import IndParams  # noqa: E402
from reference.scalp import ScalpEngine, ScalpParams  # noqa: E402
from tools.compare_events import read_rows  # noqa: E402


def load_engine(folder, symbol, tf):
    path = os.path.join(folder, f"TrendBot_{symbol}_PERIOD_{tf}_bars.csv")
    digits, _, rows = read_rows(path)
    t = [int(r[0]) for r in rows]
    o, h, l, c = ([float(r[i]) for r in rows] for i in (1, 2, 3, 4))
    eng = Engine(t, o, h, l, c, Params(point=10 ** (-digits)))
    eng.run()
    return eng, digits


def params_from_config(cfg: SystemConfig, spread: float) -> ScalpParams:
    return ScalpParams(
        gate_mode=cfg.structure.htf_mode, fvg_setup=cfg.setup.fvg_setup_enabled,
        sweep_setup=cfg.setup.sweep_setup_enabled, require_bos_linked_fvg=cfg.setup.require_bos_linked_fvg,
        trigger_window_bars=cfg.setup.trigger_window_bars, sweep_setup_max_age_bars=cfg.setup.sweep_setup_max_age_bars,
        zone_max_age_bars=cfg.entry.setup_max_bars, use_premium_discount=cfg.setup.use_premium_discount,
        max_entry_range_pct=cfg.structure.max_entry_range_pct, sl_buffer_atr=cfg.entry.sl_buffer_atr,
        min_sl_atr=cfg.entry.min_sl_atr, max_sl_atr=cfg.entry.max_sl_atr, max_chase_r=cfg.entry.max_chase_r,
        target_multiplier=cfg.scalp.target_multiplier, max_spread_to_stop_ratio=cfg.scalp.max_spread_to_stop_ratio,
        spread=spread, enabled_indicators=tuple(cfg.confirmation.enabled_indicators),
        minimum_confirmations=cfg.confirmation.minimum_confirmations,
        ind=IndParams(**cfg.confirmation.params.model_dump()))


def fmt(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def main(argv):
    if len(argv) < 3:
        print(__doc__)
        return 2
    folder, symbol = argv[1], argv[2]
    spread = float(argv[argv.index("--spread") + 1]) if "--spread" in argv else 0.0
    cfg = SystemConfig()
    d1, _ = load_engine(folder, symbol, "D1")
    h4, _ = load_engine(folder, symbol, "H4")
    m15, _ = load_engine(folder, symbol, "M15")
    m5, digits = load_engine(folder, symbol, "M5")
    sp = params_from_config(cfg, spread)
    if "--ind" in argv:  # e.g. --ind RSI,MACD  (use --ind NONE for structure only)
        names = argv[argv.index("--ind") + 1]
        sp.enabled_indicators = () if names == "NONE" else tuple(names.split(","))
        sp.minimum_confirmations = min(sp.minimum_confirmations, len(sp.enabled_indicators))
    if "--min" in argv:
        sp.minimum_confirmations = int(argv[argv.index("--min") + 1])
    sig = ScalpEngine(d1, h4, m15, m5, sp).run()
    gates = Counter()
    for t in range(m5.n):
        close_t = m5.tm[t] + 300
        g = gate(htf_trend_at(d1.tm, d1.trend_at, 86400, close_t), htf_trend_at(h4.tm, h4.trend_at, 14400, close_t), sp.gate_mode)
        gates[g] += 1
    out = os.path.join(folder, f"scalp_signals_{symbol}.csv")
    with open(out, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["time_server", "type", "dir", "price", "price2", "price3", "reason", "indicators"])
        for s in sig:
            w.writerow([fmt(s.time), s.type, s.dir, f"{s.price:.{digits}f}", f"{s.price2:.{digits}f}",
                        f"{s.price3:.{digits}f}", s.reason, s.detail])
    n = m5.n
    print(f"M5 bars: {n}   {fmt(m5.tm[0])} -> {fmt(m5.tm[-1])} (server time)")
    print(f"Gate: BULL {gates[1] / n:.0%}   BEAR {gates[-1] / n:.0%}   NO TRADE {gates[0] / n:.0%}")
    print("Signals:", dict(Counter(s.type for s in sig)))
    print("Rejections:", dict(Counter(s.reason for s in sig if s.type == "REJECT")))
    print("Setup ends:", dict(Counter(s.reason for s in sig if s.type == "SETUP_END")))
    print("\nLast candidates:")
    for s in [x for x in sig if x.type == "CANDIDATE"][-10:]:
        side = "BUY " if s.dir == 1 else "SELL"
        print(f"  {fmt(s.time)}  {side} {s.reason:5} entry {s.price:.{digits}f}  sl {s.price2:.{digits}f}  ref-target {s.price3:.{digits}f}  [{s.detail}]")
    print(f"\nFull log: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
