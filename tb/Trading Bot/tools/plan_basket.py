"""Size a scalp basket from the exported symbol spec (ExportSymbolSpec script) - NO orders, just numbers.

Example (gold, $100 account, 10% basket risk, buy at 2000.00 with stop 1995.00, up to 3 positions):
  py tools/plan_basket.py "<folder>\\TrendBot_XAUUSDm_spec.csv" --risk-pct 10 --entry 2000 --sl 1995 --dir buy --positions 3
Equity and free margin default to the values in the spec file; override with --equity / --free-margin.
"""
import argparse
import csv
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from backend.app.sizing import SymbolSpec, plan_basket  # noqa: E402


def load_spec(path):
    raw = {}
    with open(path, newline="") as fh:
        for row in csv.reader(fh):
            if len(row) >= 2:
                raw[row[0]] = row[1]
    spec = SymbolSpec(
        symbol=raw["symbol"], point=float(raw["point"]), digits=int(raw["digits"]),
        tick_size=float(raw["tick_size"]), tick_value_profit=float(raw["tick_value_profit"]),
        tick_value_loss=float(raw["tick_value_loss"]), volume_min=float(raw["volume_min"]),
        volume_max=float(raw["volume_max"]), volume_step=float(raw["volume_step"]),
        stops_level_points=int(raw["stops_level_points"]), freeze_level_points=int(raw["freeze_level_points"]),
        commission_per_lot=float(raw.get("commission_per_lot", 0)), margin_per_lot=float(raw.get("margin_per_lot", 0)),
        trade_allowed=raw.get("trade_allowed", "1") == "1")
    return spec, raw


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("spec_csv")
    ap.add_argument("--risk-pct", type=float, required=True, help="basket risk as %% of equity")
    ap.add_argument("--entry", type=float, required=True)
    ap.add_argument("--sl", type=float, required=True)
    ap.add_argument("--dir", choices=["buy", "sell"], required=True)
    ap.add_argument("--positions", type=int, default=1)
    ap.add_argument("--split", choices=["equal", "front_loaded"], default="equal")
    ap.add_argument("--multiplier", type=float, default=3.0)
    ap.add_argument("--equity", type=float)
    ap.add_argument("--free-margin", type=float)
    a = ap.parse_args(argv)
    spec, raw = load_spec(a.spec_csv)
    equity = a.equity if a.equity is not None else float(raw.get("equity", 0))
    free_margin = a.free_margin if a.free_margin is not None else float(raw.get("free_margin", 0)) or None
    if not equity > 0 or not a.risk_pct > 0:
        print("equity and risk must be positive")
        return 2
    budget = equity * a.risk_pct / 100.0
    plan = plan_basket(spec, 1 if a.dir == "buy" else -1, a.entry, a.sl, budget, a.positions, a.split,
                       a.multiplier, free_margin)
    cur = raw.get("account_currency", "")
    print(f"{spec.symbol}: equity {equity:.2f} {cur}, basket risk {a.risk_pct}% = {budget:.2f} {cur}")
    if not plan.ok:
        print(f"NO TRADE: {plan.reason}")
        return 1
    print(f"Stop distance {plan.stop_distance:.{spec.digits}f}   positions {len(plan.positions)} (max {plan.requested_positions})")
    for i, p in enumerate(plan.positions, 1):
        print(f"  position {i}: {p.lots:.2f} lots, risk {p.risk_money:.2f} {cur}")
    print(f"Total {plan.total_lots:.2f} lots, planned risk {plan.planned_risk_money:.2f} {cur} "
          f"(budget {budget:.2f}; rounding only ever reduces it)")
    print(f"Basket target {plan.target_money:.2f} {cur} net of commission -> take-profit price "
          f"{plan.tp:.{spec.digits}f}   (x{a.multiplier} of planned risk)")
    if plan.margin_required:
        print(f"Estimated margin {plan.margin_required:.2f} {cur}   (free margin: {free_margin})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
