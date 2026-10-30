"""Backend schema vs shared contract vs generated EA header: any drift fails here."""
import json
import os
import subprocess
import sys
import typing

import pytest
from pydantic import ValidationError

from backend.app import config_schema as cs

ROOT = os.path.join(os.path.dirname(__file__), "..")
CONTRACT = json.load(open(os.path.join(ROOT, "config", "contract.json")))


def literal_args(model, field):
    ann = model.model_fields[field].annotation
    if typing.get_origin(ann) is typing.Literal:
        return list(typing.get_args(ann))
    return [e.value for e in ann]  # Enum


def test_schema_version_matches():
    assert cs.SCHEMA_VERSION == CONTRACT["schema_version"]


def test_enums_match():
    e = CONTRACT["enums"]
    assert literal_args(cs.SystemConfig, "mode") == e["mode"]
    assert literal_args(cs.EntryConfig, "entry_mode") == e["entry_mode"]
    assert literal_args(cs.StructureConfig, "htf_mode") == e["htf_mode"]
    assert literal_args(cs.StructureConfig, "execution_tf") == e["execution_tf"]
    assert literal_args(cs.BreakEvenConfig, "method") == e["be_method"]
    assert literal_args(cs.StructureConfig, "setup_tf") == e["setup_tf"]
    assert literal_args(cs.ScalpConfig, "timeout_action") == e["timeout_action"]
    assert list(typing.get_args(cs.IndicatorName)) == e["indicators"]


def test_defaults_match_contract():
    d, c = CONTRACT["defaults"], cs.SystemConfig()
    assert c.mode.value == d["mode"] and c.live_confirmed is d["live_confirmed"]
    assert c.risk.risk_percent == d["risk_percent"]
    assert c.risk.max_open_risk_percent == d["max_open_risk_percent"]
    assert c.risk.max_simultaneous_positions == d["max_simultaneous_positions"]
    assert c.scalp.target_multiplier == d["target_multiplier"]
    assert c.structure.execution_tf == d["execution_tf"] and c.structure.setup_tf == d["setup_tf"]
    assert c.scalp.scalp_window_minutes == d["scalp_window_minutes"]
    assert c.scalp.timeout_action == d["timeout_action"]
    assert c.scalp.max_positions_per_basket == d["max_positions_per_basket"]
    assert c.confirmation.minimum_confirmations == d["minimum_confirmations"]
    assert len(c.confirmation.enabled_indicators) == d["enabled_indicator_count"]
    assert c.break_even.method == d["break_even_method"]
    assert c.structure.htf_mode == d["htf_mode"]
    assert c.entry.entry_mode == d["entry_mode"]
    assert c.liquidity.sweep_required is d["sweep_required"]
    assert c.daily.reset_timezone == d["reset_timezone"] and c.daily.reset_time == d["reset_time"]
    assert c.ea_payload()["derived"]["reset_utc_offset_seconds"] == d["reset_utc_offset_seconds"]
    assert c.daily.max_trades_per_day == d["max_trades_per_day"]
    assert c.daily.lockout_after_losses == d["lockout_after_losses"]
    assert c.broker.name == d["broker"] and c.broker.symbol_discovery == "terminal"


def test_scalp_ranges_match_contract():
    r = CONTRACT["ranges"]
    lo, hi = r["target_multiplier"]
    cs.ScalpConfig(target_multiplier=lo)
    cs.ScalpConfig(target_multiplier=hi)
    for bad in (lo - 0.01, hi + 0.01):
        with pytest.raises(ValidationError):
            cs.ScalpConfig(target_multiplier=bad)
    wlo, whi = r["scalp_window_minutes"]
    cs.ScalpConfig(scalp_window_minutes=wlo)
    cs.ScalpConfig(scalp_window_minutes=whi)
    for bad in (wlo - 1, whi + 1):
        with pytest.raises(ValidationError):
            cs.ScalpConfig(scalp_window_minutes=bad)
    plo, phi = r["max_positions_per_basket"]
    cs.ScalpConfig(max_positions_per_basket=plo)
    cs.ScalpConfig(max_positions_per_basket=phi)
    for bad in (plo - 1, phi + 1):
        with pytest.raises(ValidationError):
            cs.ScalpConfig(max_positions_per_basket=bad)


def _build(path, value):
    data = {}
    node = data
    for key in path[:-1]:
        node = node.setdefault(key, {})
    node[path[-1]] = value
    return cs.SystemConfig(**data)


@pytest.mark.parametrize("case", CONTRACT["invalid_cases"], ids=lambda c: c["name"])
def test_backend_rejects_every_shared_invalid_case(case):
    with pytest.raises(ValidationError):
        _build(case["path"], case["value"])


def test_generated_ea_header_is_up_to_date():
    result = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "gen_mql5.py"), "--check"])
    assert result.returncode == 0, "run: py tools/gen_mql5.py"


def test_ea_header_contains_agreed_defaults():
    text = open(os.path.join(ROOT, "ea", "Include", "TrendBotContract.mqh")).read()
    for needle in ('TB_DEF_MODE = "SIGNAL_ONLY"', 'TB_DEF_HTF_MODE = "H4_AND_D1"',
                   "TB_DEF_RESET_OFFSET = 3600", "TB_DEF_RESET_MINUTES = 0", "TB_DEF_TARGET_MULT = 3.0",
                   'TB_DEF_ENTRY_MODE = "market_confirmed"', 'TB_DEF_SETUP_TF = "M15"', 'TB_DEF_EXEC_TF = "M5"',
                   "TB_DEF_WINDOW = 15", 'TB_DEF_TIMEOUT = "CLOSE_ALL"', "TB_DEF_MIN_CONF = 3",
                   "TB_DEF_SWEEP_REQUIRED = false"):
        assert needle in text


def test_live_and_safety_rules():
    with pytest.raises(ValidationError):
        cs.SystemConfig(mode="LIVE")
    assert cs.SystemConfig().mode == cs.Mode.SIGNAL_ONLY


def test_instruments_not_hardcoded():
    c = cs.SystemConfig()
    assert c.instruments.watchlist == []
    assert cs.InstrumentConfig(symbol="XAUUSD").broker_symbol is None  # filled only by terminal discovery
