"""Generate ea/Include/TrendBotContract.mqh from config/contract.json.

Run:  py tools/gen_mql5.py          (writes the file)
      py tools/gen_mql5.py --check  (exit 1 if the committed file is stale)
"""
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONTRACT = ROOT / "config" / "contract.json"
OUT = ROOT / "ea" / "Include" / "TrendBotContract.mqh"


def q(s):
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def sarr(name, items):
    return f"const string {name}[] = {{{', '.join(q(i) for i in items)}}};"


def build() -> str:
    c = json.loads(CONTRACT.read_text())
    L = ["// AUTO-GENERATED from config/contract.json by tools/gen_mql5.py. DO NOT EDIT.",
         "#property strict", "#ifndef TB_CONTRACT_MQH", "#define TB_CONTRACT_MQH", "",
         f"#define TB_SCHEMA_VERSION {c['schema_version']}", ""]
    for k, v in c["enums"].items():
        L.append(sarr(f"TB_ENUM_{k.upper()}", v))
    r = c["ranges"]
    L += ["", f"const double TB_TARGET_MULT_MIN = {r['target_multiplier'][0]};",
          f"const double TB_TARGET_MULT_MAX = {r['target_multiplier'][1]};",
          f"const int TB_WINDOW_MIN = {r['scalp_window_minutes'][0]};",
          f"const int TB_WINDOW_MAX = {r['scalp_window_minutes'][1]};",
          f"const int TB_BASKET_POS_MIN = {r['max_positions_per_basket'][0]};",
          f"const int TB_BASKET_POS_MAX = {r['max_positions_per_basket'][1]};",
          f"const int TB_RESET_MINUTES_MIN = {r['reset_minutes'][0]};",
          f"const int TB_RESET_MINUTES_MAX = {r['reset_minutes'][1]};",
          f"const int TB_OFFSET_MIN = {r['reset_utc_offset_seconds'][0]};",
          f"const int TB_OFFSET_MAX = {r['reset_utc_offset_seconds'][1]};"]
    m = c["minimums"]
    L += [f"const double TB_RISK_MIN_EXCL = {m['risk_percent_exclusive']};",
          f"const double TB_OPEN_RISK_MIN_EXCL = {m['max_open_risk_percent_exclusive']};",
          f"const int TB_MIN_POSITIONS = {m['max_simultaneous_positions']};",
          f"const int TB_MIN_TRADES_PER_DAY = {m['max_trades_per_day']};",
          f"const int TB_MIN_LOCKOUT_LOSSES = {m['lockout_after_losses']};", ""]
    d = c["defaults"]
    L += [f"const string TB_DEF_MODE = {q(d['mode'])};", f"const double TB_DEF_RISK = {d['risk_percent']};",
          f"const double TB_DEF_OPEN_RISK = {d['max_open_risk_percent']};",
          f"const int TB_DEF_POSITIONS = {d['max_simultaneous_positions']};",
          f"const double TB_DEF_TARGET_MULT = {d['target_multiplier']};",
          f"const string TB_DEF_EXEC_TF = {q(d['execution_tf'])};",
          f"const string TB_DEF_SETUP_TF = {q(d['setup_tf'])};",
          f"const int TB_DEF_WINDOW = {d['scalp_window_minutes']};",
          f"const string TB_DEF_TIMEOUT = {q(d['timeout_action'])};",
          f"const int TB_DEF_BASKET_POS = {d['max_positions_per_basket']};",
          f"const int TB_DEF_MIN_CONF = {d['minimum_confirmations']};",
          f"const int TB_DEF_ENABLED_COUNT = {d['enabled_indicator_count']};",
          f"const string TB_DEF_HTF_MODE = {q(d['htf_mode'])};",
          f"const string TB_DEF_ENTRY_MODE = {q(d['entry_mode'])};",
          f"const bool TB_DEF_SWEEP_REQUIRED = {str(d['sweep_required']).lower()};",
          f"const string TB_DEF_RESET_TZ = {q(d['reset_timezone'])};",
          f"const int TB_DEF_RESET_OFFSET = {d['reset_utc_offset_seconds']};",
          f"const int TB_DEF_RESET_MINUTES = {int(d['reset_time'][:2]) * 60 + int(d['reset_time'][3:])};",
          f"const int TB_DEF_MAX_TRADES = {d['max_trades_per_day']};",
          f"const int TB_DEF_LOCKOUT_LOSSES = {d['lockout_after_losses']};", ""]
    v = c["trading_day_vectors"]
    ep, off, rm, day = [], [], [], []
    for x in v:
        dt = datetime.strptime(x["utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        ep.append(int(dt.timestamp()))
        off.append(x["offset"])
        rm.append(int(x["reset"][:2]) * 60 + int(x["reset"][3:]))
        day.append((date.fromisoformat(x["label"]) - date(1970, 1, 1)).days)
    L += [f"#define TB_VEC_COUNT {len(v)}",
          f"const long TB_VEC_EPOCH[] = {{{', '.join(map(str, ep))}}};",
          f"const int TB_VEC_OFFSET[] = {{{', '.join(map(str, off))}}};",
          f"const int TB_VEC_RESET_MIN[] = {{{', '.join(map(str, rm))}}};",
          f"const long TB_VEC_DAY[] = {{{', '.join(map(str, day))}}};", ""]
    ic = c["invalid_cases"]
    L += [f"#define TB_INVALID_COUNT {len(ic)}",
          sarr("TB_INVALID_FIELD", [x["path"][-1] for x in ic]),
          sarr("TB_INVALID_VALUE", [x["value"] for x in ic]), "", "#endif", ""]
    return "\n".join(L)


if __name__ == "__main__":
    text = build()
    if "--check" in sys.argv:
        sys.exit(0 if OUT.exists() and OUT.read_text() == text else 1)
    OUT.write_text(text)
    print("wrote", OUT)
