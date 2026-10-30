"""Parity check for the scalp review: MT5 indicator (TrendBotScalpReview) vs the Python reference.

1. Attach TrendBotScalpReview to a chart (CSV export on). It writes into MQL5\\Files:
     TrendBot_<SYMBOL>_PERIOD_{D1,H4,M15,M5}_bars.csv  and  TrendBot_<SYMBOL>_scalp_signals.csv
2. Copy those 5 files to one folder and run:
     py tools/compare_scalp.py "<folder>" <SYMBOL>
The bars are fed to reference/engine.py + reference/scalp.py with the parameters the indicator wrote,
and every signal (time, type, dir, prices, reason, indicator detail) is compared.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from reference.scalp import ScalpEngine, ScalpParams  # noqa: E402
from tools.compare_events import read_rows  # noqa: E402
from tools.run_scalp_reference import load_engine  # noqa: E402

BOOLS = {"fvg_setup", "sweep_setup", "require_bos_linked_fvg", "use_premium_discount"}
INTS = {"trigger_window_bars", "sweep_setup_max_age_bars", "zone_max_age_bars", "minimum_confirmations"}


def params_from_file(raw) -> ScalpParams:
    p = ScalpParams()
    for k, v in raw.items():
        if k == "gate_h4_only":
            p.gate_mode = "H4_ONLY" if int(v) else "H4_AND_D1"
        elif k == "enabled":
            p.enabled_indicators = tuple(x for x in v.split("|") if x)
        elif k in BOOLS:
            setattr(p, k, bool(int(v)))
        elif k in INTS:
            setattr(p, k, int(v))
        elif hasattr(p, k):
            setattr(p, k, float(v))
    return p


def compare(folder, symbol, signals_path=None, max_report=15):
    signals_path = signals_path or os.path.join(folder, f"TrendBot_{symbol}_scalp_signals.csv")
    d1, _ = load_engine(folder, symbol, "D1")
    h4, _ = load_engine(folder, symbol, "H4")
    m15, _ = load_engine(folder, symbol, "M15")
    m5, _ = load_engine(folder, symbol, "M5")
    digits, raw, mt_rows = read_rows(signals_path)
    py = ScalpEngine(d1, h4, m15, m5, params_from_file(raw)).run()
    tol = 1.5 * 10 ** (-digits)
    mism = []
    for i in range(max(len(py), len(mt_rows))):
        if i >= len(py):
            mism.append((i, "extra signal in MT5", mt_rows[i], None))
            continue
        if i >= len(mt_rows):
            mism.append((i, "signal missing in MT5", None, py[i]))
            continue
        m, s = mt_rows[i], py[i]
        same = (int(m[0]) == s.time and m[1] == s.type and int(m[2]) == s.dir
                and abs(float(m[3]) - s.price) <= tol and abs(float(m[4]) - s.price2) <= tol
                and abs(float(m[5]) - s.price3) <= tol and m[6] == s.reason
                and (m[7] if len(m) > 7 else "") == s.detail)
        if not same:
            mism.append((i, "differs", m, s))
    return len(py), len(mt_rows), mism


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    n_py, n_mt, mism = compare(sys.argv[1], sys.argv[2])
    print(f"python signals: {n_py}   MT5 signals: {n_mt}   mismatches: {len(mism)}")
    for i, why, m, s in mism[:15]:
        print(f"#{i} {why}\n   MT5   : {m}\n   Python: {s}")
    print("PARITY OK" if not mism else "PARITY FAILED")
    sys.exit(0 if not mism else 1)
