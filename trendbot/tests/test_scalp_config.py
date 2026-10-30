import pytest
from pydantic import ValidationError

from backend.app import config_schema as cs
from backend.app.basket import basket_budget
from backend.app.config_schema import (ConfirmationConfig, RiskConfig, ScalpConfig, StructureConfig,
                                       SystemConfig, migrate_config)


def cfg(**kw):
    return SystemConfig(**kw)


def test_scalp_defaults():
    c = cfg()
    assert c.structure.htf_mode == "H4_AND_D1"
    assert (c.structure.setup_tf, c.structure.execution_tf) == ("M15", "M5")
    assert c.entry.entry_mode == "market_confirmed"
    assert c.scalp.timeout_action == "CLOSE_ALL"
    assert c.break_even.method == "off"
    assert c.confirmation.price_action_mandatory is True
    assert c.confirmation.minimum_confirmations == 3
    assert "CCI" not in c.confirmation.enabled_indicators  # optional, off by default
    assert c.mode == cs.Mode.SIGNAL_ONLY


def test_presets_are_all_valid():
    for m in cs.TARGET_PRESETS:
        assert ScalpConfig(target_multiplier=m)
    for w in cs.WINDOW_PRESETS:
        assert ScalpConfig(scalp_window_minutes=w)
    for t in cs.BE_TRIGGER_PRESETS:
        assert cs.BreakEvenConfig(method="A", trigger_r=t)


@pytest.mark.parametrize("action", ["CLOSE_ALL", "MOVE_TO_BREAKEVEN", "KEEP_RUNNING", "CLOSE_IF_LOSS"])
def test_timeout_actions(action):
    assert ScalpConfig(timeout_action=action).timeout_action == action


def test_custom_values_validated():
    assert ScalpConfig(target_multiplier=2.5, scalp_window_minutes=45)
    for bad in ({"target_multiplier": -1}, {"scalp_window_minutes": -5}, {"timeout_action": "x"}):
        with pytest.raises(ValidationError):
            ScalpConfig(**bad)


def test_timeframe_roles():
    with pytest.raises(ValidationError):
        StructureConfig(execution_tf="M5", setup_tf="M5")
    with pytest.raises(ValidationError):
        StructureConfig(execution_tf="M1", setup_tf="M5")  # M5 not an allowed setup tf
    assert StructureConfig(execution_tf="M1", setup_tf="M15")


def test_indicator_rules():
    assert ConfirmationConfig(enabled_indicators=["RSI", "MACD"], minimum_confirmations=2)
    assert ConfirmationConfig(enabled_indicators=[], minimum_confirmations=0)  # price action only
    with pytest.raises(ValidationError):
        ConfirmationConfig(enabled_indicators=["RSI"], minimum_confirmations=2)
    with pytest.raises(ValidationError):
        ConfirmationConfig(enabled_indicators=["RSI", "RSI"], minimum_confirmations=1)
    with pytest.raises(ValidationError):
        ConfirmationConfig(enabled_indicators=["ADX"])
    with pytest.raises(ValidationError):
        ConfirmationConfig(price_action_mandatory=False)
    with pytest.raises(ValidationError):
        cs.IndicatorParams(macd_fast=26, macd_slow=12)


def test_basket_positions_cannot_exceed_global_cap():
    with pytest.raises(ValidationError):
        cfg(scalp=ScalpConfig(max_positions_per_basket=3))  # risk.max_simultaneous_positions is 1
    c = cfg(scalp=ScalpConfig(max_positions_per_basket=3), risk=RiskConfig(max_simultaneous_positions=3))
    assert c.scalp.max_positions_per_basket == 3


def test_basket_max_loss_cannot_exceed_basket_risk():
    with pytest.raises(ValidationError):
        cfg(scalp=ScalpConfig(basket_max_loss_percent=2.0))  # basket risk is 1%
    assert cfg(scalp=ScalpConfig(basket_max_loss_percent=0.8))


def test_budget_example_from_spec():
    c = cfg(risk=RiskConfig(risk_percent=10, max_open_risk_percent=10))
    b = basket_budget(100.0, c)
    assert b.risk_money == 10.0 and b.max_loss_money == 10.0
    assert b.target_money == 30.0 and b.target_percent_of_equity == 30.0


def test_multiple_positions_do_not_multiply_risk():
    c = cfg(risk=RiskConfig(risk_percent=10, max_open_risk_percent=10, max_simultaneous_positions=3),
            scalp=ScalpConfig(max_positions_per_basket=3))
    b = basket_budget(100.0, c)
    assert b.risk_money == 10.0  # one budget for the whole basket, not 3 x 10


def test_budget_rejects_bad_equity():
    for eq in (0, -5):
        with pytest.raises(ValueError):
            basket_budget(eq, cfg())


def test_old_pending_entry_modes_rejected():
    for old in ("fvg_50", "fvg_edge", "market_on_touch"):
        with pytest.raises(ValidationError):
            cs.EntryConfig(entry_mode=old)


def test_rr_section_is_gone():
    with pytest.raises(ValidationError):
        cfg(rr={"target_rr": 3.0})


def test_migrate_v2_to_v3():
    v2 = {"schema_version": 2, "rr": {"target_rr": 3.0}, "entry": {"entry_mode": "fvg_50", "sl_buffer_atr": 0.2},
          "structure": {"execution_tf": "M15", "htf_mode": "H4_AND_D1"}, "derived": {}, "config_hash": "x"}
    new = SystemConfig(**migrate_config(v2))
    assert new.entry.entry_mode == "market_confirmed" and new.entry.sl_buffer_atr == 0.2
    assert new.structure.execution_tf == "M5" and new.structure.setup_tf == "M15"
    assert new.mode == cs.Mode.SIGNAL_ONLY
    assert migrate_config({"schema_version": 3}) == {"schema_version": 3}
    with pytest.raises(ValueError):
        migrate_config({"schema_version": 1})


def test_ea_payload_has_scalp_window_seconds():
    assert cfg().ea_payload()["derived"]["scalp_window_seconds"] == 900


def test_setup_config_defaults_and_rules():
    c = cfg()
    assert c.setup.trigger_window_bars == 12 and c.setup.require_bos_linked_fvg is True
    with pytest.raises(ValidationError):
        cs.SetupConfig(fvg_setup_enabled=False, sweep_setup_enabled=False)
    with pytest.raises(ValidationError):
        cs.SetupConfig(trigger_window_bars=0)
    assert cs.SetupConfig(fvg_setup_enabled=False)
