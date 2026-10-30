"""Parity check: MT5 indicator output vs the Python reference engine.

1. Attach TrendBotStageA to a chart (CSV export on). Files appear in MT5 > File > Open Data Folder > MQL5\\Files:
     TrendBot_<SYMBOL>_<TF>_bars.csv   and   TrendBot_<SYMBOL>_<TF>_events.csv
2. Run:  py tools/compare_events.py path\\to\\..._bars.csv path\\to\\..._events.csv
The bars are fed to reference/engine.py with the same parameters the indicator wrote into the
events file, then every event (time, type, dir, price, price2, flag, trend) is compared.
"""
import csv
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from reference.engine import Engine, Params  # noqa: E402

MIT = {1: "touch", 2: "half", 3: "full"}
INT_FIELDS = {"swing_left", "swing_right", "disp_max_bars", "sweep_confirm_bars", "fvg_min_points", "fvg_max_age"}


def read_rows(path):
    digits, params, rows = 5, {}, []
    with open(path, newline="") as fh:
        for row in csv.reader(fh):
            if not row:
                continue
            if row[0].startswith("#digits="):
                digits = int(row[0].split("=")[1])
            elif row[0] == "#params":
                for kv in row[1:]:
                    k, v = kv.split("=")
                    params[k] = v
            elif row[0].startswith("#"):
                continue
            else:
                rows.append(row)
    return digits, params, rows[1:]  # drop header row


def build_params(raw, digits):
    p = Params()
    for k, v in raw.items():
        if k == "fvg_mit_level":
            p.fvg_mitigation = MIT[int(v)]
        elif k in INT_FIELDS:
            setattr(p, k, int(v))
        elif hasattr(p, k):
            setattr(p, k, float(v))
    return p


def compare(bars_path, events_path, max_report=15):
    digits, _, bars = read_rows(bars_path)
    d2, raw, mt_rows = read_rows(events_path)
    p = build_params(raw, d2)
    t = [int(r[0]) for r in bars]
    o, h, l, c = ([float(r[i]) for r in bars] for i in (1, 2, 3, 4))
    py = Engine(t, o, h, l, c, p).run()
    tol = 1.5 * 10 ** (-digits)
    mism = []
    for i in range(max(len(py), len(mt_rows))):
        if i >= len(py):
            mism.append((i, "extra event in MT5", mt_rows[i], None))
            continue
        if i >= len(mt_rows):
            mism.append((i, "event missing in MT5", None, py[i]))
            continue
        m, e = mt_rows[i], py[i]
        same = (int(m[0]) == e.time and m[1] == e.type and int(m[2]) == e.dir and int(m[5]) == e.flag
                and int(m[6]) == e.trend and abs(float(m[3]) - e.price) <= tol and abs(float(m[4]) - e.price2) <= tol)
        if not same:
            mism.append((i, "differs", m, e))
    return len(py), len(mt_rows), mism


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(2)
    n_py, n_mt, mism = compare(sys.argv[1], sys.argv[2])
    print(f"python events: {n_py}   MT5 events: {n_mt}   mismatches: {len(mism)}")
    for i, why, m, e in mism[:15]:
        print(f"#{i} {why}\n   MT5   : {m}\n   Python: {e}")
    print("PARITY OK" if not mism else "PARITY FAILED")
    sys.exit(0 if not mism else 1)
