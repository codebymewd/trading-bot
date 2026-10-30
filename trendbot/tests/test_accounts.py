"""Multi-account registry tests (requirement #16/#17/#33): isolation, identity verification,
activation/pause, unlimited accounts, persistence across restarts."""
from backend.app.accounts import AccountStore, prop_from_account
from backend.app.db import Db


class Clock:
    def __init__(self):
        self.t = 1_700_000_000.0

    def __call__(self):
        return self.t


def store():
    db = Db(":memory:")
    return db, AccountStore(db, Clock())


def test_default_account_created_on_first_run():
    _, s = store()
    assert [a.id for a in s.list()] == ["default"]


def test_create_many_accounts_no_hard_limit():
    _, s = store()
    for i in range(25):
        s.create(name=f"FTMO {i}", mt5_login=10000 + i, broker="FTMO", initial_balance=10_000)
    assert len(s.list()) == 26          # default + 25


def test_duplicate_name_rejected():
    _, s = store()
    s.create(name="Alpha")
    try:
        s.create(name="alpha")
        assert False, "duplicate name must be rejected"
    except ValueError:
        pass


def test_invalid_login_rejected():
    _, s = store()
    try:
        s.create(name="Bad", mt5_login=-5)
        assert False, "negative login must be rejected"
    except ValueError:
        pass


def test_isolation_state_per_account():
    _, s = store()
    a = s.create(name="A", mt5_login=111)
    b = s.create(name="B", mt5_login=222)
    s.verify_ea(a.id, 111)
    assert s.ea_state(a.id).get("ea_verified_login") == 111
    assert s.ea_state(b.id).get("ea_verified_login") is None      # never mixed


def test_ea_identity_mismatch_refused():
    db, s = store()
    a = s.create(name="A", mt5_login=111)
    assert s.verify_ea(a.id, 999) is None                          # EA reports wrong login -> refuse
    kinds = [r["action"] for r in db.audit_log(limit=10)]
    assert "EA_IDENTITY_MISMATCH" in kinds
    assert s.verify_ea(a.id, 111) is not None                      # right login -> verified


def test_unconfigured_login_gets_verified_and_recorded():
    _, s = store()
    a = s.create(name="A")
    assert s.verify_ea(a.id, 555) is not None
    assert s.ea_state(a.id)["ea_verified_login"] == 555            # real EA-reported identity


def test_activation_pause_archive_flow():
    _, s = store()
    a = s.create(name="A", mt5_login=111)
    assert a.status == "PAUSED" and a.active is False              # new account starts paused
    act = s.activate(a.id)
    assert act.status == "ACTIVE" and act.active is True
    p = s.pause(a.id)
    assert p.status == "PAUSED" and p.active is False
    s.archive(a.id)
    assert s.get(a.id).archived is True
    assert a.id not in [x.id for x in s.list()]                    # hidden from active list
    try:
        s.activate(a.id)
        assert False
    except ValueError:
        pass


def test_default_account_cannot_be_archived():
    _, s = store()
    try:
        s.archive("default")
        assert False
    except ValueError:
        pass


def test_persistence_across_restart():
    db = Db(":memory:")
    s1 = AccountStore(db, Clock())
    a = s1.create(name="KeepMe", mt5_login=4242, initial_balance=10_000)
    s1.activate(a.id)
    s2 = AccountStore(db, Clock())                                  # simulated backend restart
    kept = s2.get(a.id)
    assert kept.name == "KeepMe" and kept.mt5_login == 4242 and kept.status == "ACTIVE"


def test_effective_config_overrides_isolated():
    from backend.app.config_schema import SystemConfig
    base = SystemConfig()                                            # validated defaults (strict schema)
    orig = base.risk.risk_percent
    _, s = store()
    # override BOTH coupled risk fields so the strict cross-validation stays satisfied
    a = s.create(name="Override", overrides={"sections": {"risk": {
        "risk_percent": orig * 2, "max_open_risk_percent": max(base.risk.max_open_risk_percent, orig * 4)}}})
    eff = s.effective_config(a, base)
    assert eff.risk.risk_percent == orig * 2
    assert base.risk.risk_percent == orig                           # global config untouched
    dflt = s.effective_config(s.get("default"), base)
    assert dflt.risk.risk_percent == orig                           # default mirrors legacy behaviour


def test_prop_profile_selection():
    from backend.app.config_schema import PropProfile
    prof = PropProfile(name="ftmo10k", confirmed=True, starting_balance=10_000,
                       max_drawdown_percent=10.0, daily_loss_limit_percent=5.0)

    class FakeCfg:
        active_prop_profile = "ftmo10k"
        prop_profiles = {"ftmo10k": prof}

    _, s = store()
    a = s.create(name="P", prop_profile="ftmo10k")
    resolved = prop_from_account(a, FakeCfg())
    assert resolved is not None and resolved.starting_balance == 10_000
