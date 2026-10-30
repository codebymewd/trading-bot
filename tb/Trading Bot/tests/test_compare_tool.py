import csv
import os

from reference.engine import Engine, Params
from tests.test_engine import random_bars
from tools.compare_events import compare

DIGITS = 2


def write_files(tmp_path, tamper=False):
    t, o, h, l, c = random_bars(500, 7)
    r = lambda x: round(x, DIGITS)
    o, h, l, c = ([r(v) for v in col] for col in (o, h, l, c))
    bars = tmp_path / "b.csv"
    with open(bars, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow([f"#digits={DIGITS}"])
        w.writerow(["time", "open", "high", "low", "close"])
        for row in zip(t, o, h, l, c):
            w.writerow([row[0]] + [f"{v:.{DIGITS}f}" for v in row[1:]])
    ev = Engine(t, o, h, l, c, Params()).run()
    events = tmp_path / "e.csv"
    with open(events, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow([f"#digits={DIGITS}"])
        w.writerow(["#params", "swing_left=3", "swing_right=3", "fvg_mit_level=3", "min_swing_atr=0.5"])
        w.writerow(["time", "type", "dir", "price", "price2", "flag", "trend", "gate"])
        for i, e in enumerate(ev):
            price = e.price + (5.0 if tamper and i == 10 else 0.0)
            w.writerow([e.time, e.type, e.dir, f"{price:.{DIGITS}f}", f"{e.price2:.{DIGITS}f}", e.flag, e.trend, 0])
    return str(bars), str(events), len(ev)


def test_parity_ok(tmp_path):
    b, e, n = write_files(tmp_path)
    n_py, n_mt, mism = compare(b, e)
    assert n_py == n_mt == n and n > 20 and mism == []


def test_parity_detects_difference(tmp_path):
    b, e, _ = write_files(tmp_path, tamper=True)
    _, _, mism = compare(b, e)
    assert len(mism) == 1 and mism[0][0] == 10
