import csv
from collections import Counter

from reference.scalp import ScalpEngine, ScalpParams
from tests.test_engine import random_bars
from tests.test_scalp_engine import aggregate
from tools.compare_scalp import compare
from tools.run_scalp_reference import load_engine

SYM = "TEST"
DIGITS = 2


def write_bars(folder, tf, b):
    with open(folder / f"TrendBot_{SYM}_PERIOD_{tf}_bars.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow([f"#digits={DIGITS}"])
        w.writerow(["time", "open", "high", "low", "close"])
        for r in zip(*b):
            w.writerow([r[0]] + [f"{v:.{DIGITS}f}" for v in r[1:]])


def make_folder(tmp_path, tamper=False):
    bars = random_bars(288 * 90, 4)
    write_bars(tmp_path, "M5", bars)
    write_bars(tmp_path, "M15", aggregate(bars, 3))
    write_bars(tmp_path, "H4", aggregate(bars, 48))
    write_bars(tmp_path, "D1", aggregate(bars, 288))
    d1, h4, m15, m5 = (load_engine(str(tmp_path), SYM, tf)[0] for tf in ("D1", "H4", "M15", "M5"))
    p = ScalpParams(enabled_indicators=("RSI", "MACD", "BB", "STOCH"), minimum_confirmations=3)
    sig = ScalpEngine(d1, h4, m15, m5, p).run()
    out = tmp_path / f"TrendBot_{SYM}_scalp_signals.csv"
    with open(out, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow([f"#digits={DIGITS}"])
        w.writerow(["#params", "gate_h4_only=0", "fvg_setup=1", "sweep_setup=1", "require_bos_linked_fvg=1",
                    "trigger_window_bars=12", "sweep_setup_max_age_bars=12", "zone_max_age_bars=24",
                    "use_premium_discount=1", "max_entry_range_pct=50.0", "sl_buffer_atr=0.1", "min_sl_atr=0.3",
                    "max_sl_atr=3.0", "max_chase_r=0.5", "target_multiplier=3.0", "max_spread_to_stop_ratio=0.15",
                    "spread=0.0", "enabled=RSI|MACD|BB|STOCH", "minimum_confirmations=3"])
        w.writerow(["time", "type", "dir", "price", "price2", "price3", "reason", "detail"])
        for i, s in enumerate(sig):
            reason = s.reason + ("x" if tamper and i == 5 else "")
            w.writerow([s.time, s.type, s.dir, f"{s.price:.{DIGITS}f}", f"{s.price2:.{DIGITS}f}",
                        f"{s.price3:.{DIGITS}f}", reason, s.detail])
    return len(sig), Counter(s.type for s in sig)


def test_parity_ok(tmp_path):
    n, kinds = make_folder(tmp_path)
    assert n > 100 and kinds["REJECT"] > 0
    n_py, n_mt, mism = compare(str(tmp_path), SYM)
    assert n_py == n_mt == n and mism == []


def test_parity_detects_difference(tmp_path):
    make_folder(tmp_path, tamper=True)
    _, _, mism = compare(str(tmp_path), SYM)
    assert len(mism) == 1 and mism[0][0] == 5
