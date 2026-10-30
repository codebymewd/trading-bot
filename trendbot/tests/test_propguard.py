import pytest

from backend.app.config_schema import PropProfile
from backend.app.propguard import PropState, prop_check


def prof(**kw):
    base = dict(name="F", confirmed=True, starting_balance=10000, daily_loss_limit_percent=5.0,
                max_drawdown_percent=10.0, safety_buffer_percent=0.5)
    base.update(kw)
    return PropProfile(**base)


def st(balance=10000, equity=10000, day_start=10000, hw=10000):
    return PropState(balance, equity, day_start, hw)


def test_no_profile_allows():
    assert prop_check(None, "XAUUSD", st(), 100, 0.1) == ""


def test_unconfirmed_or_unresolved_profile_blocks():
    assert prop_check(prof(confirmed=False), "XAUUSD", st(), 100, 0.1) == "prop_profile_not_ready"
    assert prop_check(prof(unresolved_fields=["drawdown_type"]), "XAUUSD", st(), 100, 0.1) == "prop_profile_not_ready"


def test_symbol_and_lot_limits():
    p = prof(restricted_symbols=["btcusd"], max_lot_size=0.5)
    assert prop_check(p, "BTCUSD", st(), 100, 0.1) == "prop_symbol_restricted"
    assert prop_check(p, "XAUUSD", st(), 100, 0.6) == "prop_lot_limit"
    assert prop_check(p, "XAUUSD", st(), 100, 0.5) == ""


def test_daily_loss_includes_new_basket_worst_case_and_buffer():
    p = prof()                                               # 5% of 10000 = 500, buffer 0.5% = 50
    assert prop_check(p, "X", st(equity=9700), 100, 0.1) == ""            # used 300 + 100 + 50 = 450 <= 500
    assert prop_check(p, "X", st(equity=9700), 160, 0.1) == "prop_daily_loss"   # 300 + 160 + 50 = 510 > 500
    assert prop_check(p, "X", st(equity=9400), 1, 0.1) == "prop_daily_loss"      # already near the limit


def test_daily_loss_basis_floating_vs_balance():
    p_float = prof(count_floating_pl=True)
    p_bal = prof(count_floating_pl=False)
    s = st(balance=10000, equity=9450)                        # 550 floating loss
    assert prop_check(p_float, "X", s, 10, 0.1) == "prop_daily_loss"
    assert prop_check(p_bal, "X", s, 10, 0.1) == ""            # floating P/L not counted by this firm


def test_daily_loss_reference_day_start_balance():
    p = prof(daily_loss_reference="day_start_balance", daily_loss_limit_percent=5.0, max_drawdown_percent=40.0)
    s = st(balance=8000, equity=7700, day_start=8000)         # 300 lost vs day start 8000 = 3.75%
    assert prop_check(p, "X", s, 50, 0.1) == ""
    assert prop_check(p, "X", s, 100, 0.1) == "prop_daily_loss"   # 300 + 100 = 400 = 5% of 8000 + buffer 0.5% -> over


def test_static_vs_trailing_drawdown():
    static = prof(drawdown_type="static", daily_loss_limit_percent=None)
    trailing = prof(drawdown_type="trailing", daily_loss_limit_percent=None)
    s = st(balance=10800, equity=10800, hw=11500)             # 700 below the high-water mark, still above the start
    assert prop_check(static, "X", s, 100, 0.1) == ""
    assert prop_check(trailing, "X", s, 300, 0.1) == "prop_drawdown"   # 700 + 300 + 50 > 1000
    assert prop_check(trailing, "X", s, 200, 0.1) == ""


def test_drawdown_basis_balance_vs_equity():
    pe = prof(drawdown_basis="equity", daily_loss_limit_percent=None)
    pb = prof(drawdown_basis="balance", daily_loss_limit_percent=None)
    s = st(balance=10000, equity=9100)
    assert prop_check(pe, "X", s, 100, 0.1) == "prop_drawdown"
    assert prop_check(pb, "X", s, 100, 0.1) == ""
