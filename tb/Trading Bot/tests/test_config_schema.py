import pytest
from pydantic import ValidationError
from backend.app.config_schema import *


def test_defaults_match_spec():
    c = SystemConfig()
    assert c.mode == Mode.SIGNAL_ONLY
    assert c.risk.risk_percent == 1.0
    assert c.scalp.target_multiplier == 3.0
    assert c.daily.max_trades_per_day == 2 and c.daily.lockout_after_losses == 2
    assert c.news.high.allow_entries is False
    assert c.news.on_data_unavailable == "block"
    assert c.risk.max_simultaneous_positions == 1


def test_risk_has_no_arbitrary_cap_but_must_be_positive():
    assert RiskConfig(risk_percent=3.5, max_open_risk_percent=3.5).risk_percent == 3.5
    for bad in (0, -1):
        with pytest.raises(ValidationError):
            RiskConfig(risk_percent=bad)


def test_open_risk_must_cover_trade_risk():
    with pytest.raises(ValidationError):
        SystemConfig(risk=RiskConfig(risk_percent=2.0, max_open_risk_percent=1.0))


def test_unknown_field_rejected():
    with pytest.raises(ValidationError):
        SystemConfig(foo=1)


def test_live_requires_confirmation_and_validated_instruments():
    with pytest.raises(ValidationError):
        SystemConfig(mode=Mode.LIVE)
    wl = InstrumentSelection(watchlist=[InstrumentConfig(symbol="XAUUSD", state=InstrumentState.MONITORED)])
    with pytest.raises(ValidationError):
        SystemConfig(mode=Mode.LIVE, live_confirmed=True, instruments=wl)
    wl2 = InstrumentSelection(watchlist=[InstrumentConfig(symbol="XAUUSD", state=InstrumentState.LIVE_VALIDATED)])
    assert SystemConfig(mode=Mode.LIVE, live_confirmed=True, instruments=wl2).mode == Mode.LIVE


def _profile(**kw):
    base = dict(name="FirmX", starting_balance=10000, daily_loss_limit_percent=5, max_drawdown_percent=10)
    base.update(kw)
    return PropProfile(**base)


def test_unconfirmed_or_unresolved_profile_cannot_be_active():
    with pytest.raises(ValidationError):
        SystemConfig(prop_profiles={"a": _profile()}, active_prop_profile="a")
    with pytest.raises(ValidationError):
        SystemConfig(prop_profiles={"a": _profile(confirmed=True, unresolved_fields=["drawdown_type"])},
                     active_prop_profile="a")
    assert SystemConfig(prop_profiles={"a": _profile(confirmed=True)}, active_prop_profile="a")


def test_strictest_daily_limit_with_buffer():
    c = SystemConfig(daily=DailyLimits(daily_loss_limit_percent=3.0),
                     prop_profiles={"a": _profile(confirmed=True, safety_buffer_percent=1.0)},
                     active_prop_profile="a")
    assert effective_loss_limits(c)["daily_loss_percent"] == 3.0
    c2 = SystemConfig(prop_profiles={"a": _profile(confirmed=True, safety_buffer_percent=1.0)},
                      active_prop_profile="a")
    assert effective_loss_limits(c2)["daily_loss_percent"] == 4.0  # 5 - 1


def test_break_even_off_by_default():
    assert SystemConfig().break_even.method == "off"


def test_break_even_partial_only_method_b():
    with pytest.raises(ValidationError):
        BreakEvenConfig(method="A", partial_close_percent=50)
    assert BreakEvenConfig(method="B", partial_close_percent=50)


def test_hash_stable_and_changes():
    a, b = SystemConfig(), SystemConfig()
    assert a.config_hash() == b.config_hash()
    assert a.config_hash() != SystemConfig(config_version=2).config_hash()


def test_risk_increase_detection():
    old = SystemConfig()
    new = SystemConfig(risk=RiskConfig(risk_percent=1.5, max_open_risk_percent=1.5))
    assert risk_increase_requires_confirmation(old, new)
    assert not risk_increase_requires_confirmation(new, old)


def test_bad_time_format():
    with pytest.raises(ValidationError):
        ScheduleConfig(entry_cutoff="25:00")
