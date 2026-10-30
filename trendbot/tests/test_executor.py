import pytest

from backend.app import basket_manager as bm
from backend.app import executor as ex
from backend.app.config_schema import (BrokerConfig, DailyLimits, InstrumentState, Mode, NewsConfig, PropProfile, RiskConfig,
                                       ScalpConfig, SystemConfig, SystemSafety)
from backend.app.executor import BasketStore, Candidate, GateContext
from backend.app.propguard import PropState
from backend.app.sizing import SymbolSpec
from tests.fake_broker import FakeBroker

XAU = SymbolSpec("XAUUSD", 0.01, 2, 0.01, 1.0, 1.0, 0.01, 100.0, 0.01, stops_level_points=10, margin_per_lot=805.1)
DAY = (1_700_000_000 // 86400) * 86400
NOW = DAY + 9 * 3600            # 10:00 Lagos
MPARAMS = bm.ManagerParams("A", trigger_r=1.0, be_offset_points=0, timeout_action="CLOSE_ALL")


def cfg_of(mode=Mode.DEMO, **kw):
    kw.setdefault("risk", RiskConfig(risk_percent=1.0, max_open_risk_percent=1.0, max_simultaneous_positions=3))
    kw.setdefault("scalp", ScalpConfig(max_positions_per_basket=2))
    kw.setdefault("broker", BrokerConfig(allowed_logins=[555001]))
    return SystemConfig(mode=mode, **kw)


def cand(entry=2000.2, sl=1995.0, close_time=NOW - 5, key="FVG:1000.0:1004.0", d=1):
    return Candidate(d, entry, sl, "FVG", close_time, key)


def ctx(cfg=None, broker=None, store=None, c=None, **kw):
    base = dict(cfg=cfg or cfg_of(), now=NOW, config_age_seconds=5.0, symbol="XAUUSD",
                instrument_state=InstrumentState.DEMO_ELIGIBLE, cand=c or cand(), broker=broker or FakeBroker(XAU),
                store=store or BasketStore(), news_status="clear", volatility="normal")
    base.update(kw)
    return GateContext(**base)


# ---------------------------------------------------------------- gate: each rejection reason
@pytest.mark.parametrize("override,reason", [
    (dict(cfg=cfg_of(safety=SystemSafety(kill_switch=True))), "kill_switch"),
    (dict(config_age_seconds=10_000.0), "stale_config"),
    (dict(now=DAY + 22 * 3600, c=cand(close_time=DAY + 22 * 3600 - 5)), "outside_trading_window"),
    (dict(instrument_state=InstrumentState.MONITORED), "instrument_not_validated_for_mode"),
    (dict(c=cand(close_time=NOW - 60)), "stale_signal"),
    (dict(news_status="restricted"), "news_restricted"),
    (dict(news_status="unavailable"), "news_unavailable"),
    (dict(volatility="extreme"), "volatility_extreme"),
    (dict(volatility="unknown"), "volatility_unknown"),
    (dict(c=cand(entry=1998.0, sl=1995.0)), "price_moved_since_signal"),
])
def test_gate_rejections(override, reason):
    g = ex.run_gate(ctx(**override))
    assert not g.ok and g.reason == reason


def test_gate_pass_demo_sends_orders():
    g = ex.run_gate(ctx())
    assert g.ok and g.send_orders and g.reason == ""
    assert g.plan.ok and g.live_entry == 2000.2 and g.plan.sl == 1995.0


def test_news_unavailable_can_be_allowed_explicitly():
    cfg = cfg_of(news=NewsConfig(on_data_unavailable="allow"))
    assert ex.run_gate(ctx(cfg=cfg, news_status="unavailable")).ok


def test_signal_only_runs_everything_but_never_calls_broker():
    b = FakeBroker(XAU)
    g = ex.run_gate(ctx(cfg=cfg_of(Mode.SIGNAL_ONLY), broker=b, instrument_state=InstrumentState.MONITORED))
    assert g.ok and g.reason == "signal_only" and not g.send_orders and g.plan.ok
    assert ex.execute_basket(ctx(cfg=cfg_of(Mode.SIGNAL_ONLY), broker=b), g).reason == "orders_not_permitted"
    assert b.calls == []


def test_demo_refuses_real_account_and_live_is_unavailable():
    g = ex.run_gate(ctx(broker=FakeBroker(XAU, demo=False)))
    assert not g.ok and g.reason == "demo_mode_on_live_account" and not g.send_orders
    live_cfg = cfg_of(Mode.LIVE, live_confirmed=True)
    g = ex.run_gate(ctx(cfg=live_cfg, instrument_state=InstrumentState.LIVE_VALIDATED, broker=FakeBroker(XAU, demo=False)))
    assert not g.send_orders and g.reason == "live_trading_not_available"


def test_gate_money_and_exposure_checks():
    small = FakeBroker(XAU, equity=100.0, free_margin=100.0)          # 1% = $1 risk: min lot would risk $5
    assert ex.run_gate(ctx(broker=small)).reason == "plan_rejected:min_lot_exceeds_risk"
    assert ex.run_gate(ctx(broker=FakeBroker(XAU, free_margin=5.0))).reason == "insufficient_margin"
    b = FakeBroker(XAU)
    b.margin_override = None
    assert ex.run_gate(ctx(broker=b, volatility="elevated")).ok
    no_quote = FakeBroker(XAU)
    no_quote.quote_ok = False
    assert ex.run_gate(ctx(broker=no_quote)).reason == "no_quote"
    wide = FakeBroker(XAU, bid=2000.0, ask=2003.0)
    assert ex.run_gate(ctx(broker=wide, c=cand(entry=2003.0))).reason == "spread_too_wide"
    unknown = ex.run_gate(ctx(c=cand()))
    assert unknown.ok
    store = BasketStore()
    store.counters.locked_out = True
    assert ex.run_gate(ctx(store=store)).reason == "locked_out"
    store2 = BasketStore()
    store2.counters.trades_opened = 2
    assert ex.run_gate(ctx(store=store2)).reason == "daily_basket_limit"


def test_daily_loss_limit_blocks():
    cfg = cfg_of(daily=DailyLimits(daily_loss_limit_percent=3.0))
    b = FakeBroker(XAU, equity=960.0, free_margin=960.0)
    assert ex.run_gate(ctx(cfg=cfg, broker=b, day_start_equity=1000.0)).reason == "daily_loss_limit"
    assert ex.run_gate(ctx(cfg=cfg, broker=FakeBroker(XAU, equity=985.0, free_margin=985.0), day_start_equity=1000.0)).ok


# ---------------------------------------------------------------- execution
def test_execute_opens_basket_with_broker_side_stops_and_marks_setup_used(tmp_path):
    store = BasketStore(str(tmp_path / "state.json"))
    b = FakeBroker(XAU)
    c = ctx(broker=b, store=store)
    g = ex.run_gate(c)
    r = ex.execute_basket(c, g)
    assert r.ok and len(b.sent) == len(g.plan.positions)
    assert all(s[3] == 1995.0 and s[4] == g.plan.tp for s in b.sent)          # SL and TP attached to every order
    assert all(s[5].startswith("TB|XAUUSD-") for s in b.sent)
    assert store.counters.trades_opened == 1 and len(store.open_baskets()) == 1
    assert [e["type"] for e in store.events][-1] == "BASKET_OPENED"
    again = ex.run_gate(ctx(broker=b, store=store))
    assert again.reason in ("duplicate_setup", "symbol_already_open")
    # the used-setup list survives a restart: same candidate again is a duplicate, never a second basket
    reloaded = BasketStore.load(str(tmp_path / "state.json"))
    assert ex.setup_id("XAUUSD", cand()) in reloaded.used_setups
    assert ex.run_gate(ctx(broker=b, store=reloaded)).reason == "duplicate_setup"


def test_two_positions_share_one_budget():
    b = FakeBroker(XAU, equity=2000.0, free_margin=2000.0)
    cfg = cfg_of(risk=RiskConfig(risk_percent=1.0, max_open_risk_percent=1.0, max_simultaneous_positions=3),
                 scalp=ScalpConfig(max_positions_per_basket=2))
    store = BasketStore()
    c = ctx(cfg=cfg, broker=b, store=store, c=cand(sl=1997.0, entry=2000.2))
    g = ex.run_gate(c)
    r = ex.execute_basket(c, g)
    assert r.ok and len(r.basket.positions) == 2
    assert r.basket.planned_risk_money <= 20.0 + 1e-9                         # $2000 x 1% - NOT 2 x $20


def test_first_order_rejected_opens_nothing_and_blocks_retry():
    b = FakeBroker(XAU)
    b.fail_send_at = {0}
    store = BasketStore()
    c = ctx(broker=b, store=store)
    r = ex.execute_basket(c, ex.run_gate(c))
    assert not r.ok and r.reason == "first_order_failed" and not store.open_baskets()
    assert store.counters.trades_opened == 0
    assert ex.run_gate(ctx(broker=b, store=store)).reason == "duplicate_setup"   # never retried automatically
    assert store.events[-1]["type"] == "ORDER_REJECTED"


def test_second_order_rejected_keeps_a_smaller_basket_with_recomputed_risk():
    b = FakeBroker(XAU, equity=2000.0, free_margin=2000.0)
    b.fail_send_at = {1}
    cfg = cfg_of(scalp=ScalpConfig(max_positions_per_basket=2))
    c = ctx(cfg=cfg, broker=b, store=BasketStore(), c=cand(sl=1997.0))
    g = ex.run_gate(c)
    assert len(g.plan.positions) == 2
    r = ex.execute_basket(c, g)
    assert r.ok and len(r.basket.positions) == 1
    assert r.basket.planned_risk_money < g.plan.planned_risk_money
    assert r.basket.target_money == pytest.approx(r.basket.planned_risk_money * 3.0)
    assert any(e["type"] == "BASKET_PARTIAL" for e in c.store.events)


def test_slippage_that_breaches_risk_closes_everything():
    b = FakeBroker(XAU, equity=1040.0, free_margin=1040.0)   # $10.40 budget = exactly 0.02 lots
    b.slippage = 1.5                                  # fills 1.5 worse: stop is 6.7 away -> risk +29%
    c = ctx(broker=b, store=BasketStore())
    r = ex.execute_basket(c, ex.run_gate(c))
    assert not r.ok and r.reason == "slippage_risk_breach"
    assert b.positions() == [] and not c.store.open_baskets()
    assert c.store.events[-1]["type"] == "SLIPPAGE_RISK_BREACH"


# ---------------------------------------------------------------- management
def open_one(cfg=None, positions=2):
    cfg = cfg or cfg_of(scalp=ScalpConfig(max_positions_per_basket=positions))
    b = FakeBroker(XAU, equity=2000.0, free_margin=2000.0)
    store = BasketStore()
    c = ctx(cfg=cfg, broker=b, store=store, c=cand(sl=1997.0, entry=2000.2))
    r = ex.execute_basket(c, ex.run_gate(c))
    assert r.ok
    return cfg, b, store, r.basket


def test_target_closes_everything_and_records_the_result():
    cfg, b, store, basket = open_one()
    b.bid, b.ask = basket.positions[0].tp + 0.1, basket.positions[0].tp + 0.3
    acts = ex.manage_basket(basket, b, MPARAMS, NOW + 60, store, cfg)
    assert [a.reason for a in acts] == ["target"]
    assert basket.status == "CLOSED" and b.positions() == [] and basket.final_pnl >= basket.target_money - 1e-6
    assert store.counters.realized_net == pytest.approx(basket.final_pnl)
    assert store.events[-1]["type"] == "BASKET_CLOSED" and store.events[-1]["outcome"] == "WIN"


def test_break_even_modification_is_only_confirmed_when_the_broker_accepts():
    cfg, b, store, basket = open_one(positions=2)
    entry = basket.avg_entry
    b.bid, b.ask = entry + 3.3, entry + 3.5                       # beyond 1R (stop distance is 3.2)
    b.fail_modify = True
    ex.manage_basket(basket, b, MPARAMS, NOW + 30, store, cfg)
    assert not basket.be_done and all(p.sl == 1997.0 for p in basket.positions)
    assert any(e["type"] == "SL_MODIFY_FAILED" for e in store.events)
    b.fail_modify = False
    ex.manage_basket(basket, b, MPARAMS, NOW + 31, store, cfg)    # retried next tick
    assert basket.be_done and all(p.sl == pytest.approx(entry) for p in basket.positions)
    assert any(e["type"] == "SL_MOVED" for e in store.events)


def test_close_failure_keeps_basket_open_and_retries():
    cfg, b, store, basket = open_one()
    b.fail_close = True
    ex.manage_basket(basket, b, MPARAMS, NOW + 901, store, cfg)    # timeout CLOSE_ALL
    assert basket.status == "OPEN" and any(e["type"] == "CLOSE_FAILED" for e in store.events)
    b.fail_close = False
    ex.manage_basket(basket, b, MPARAMS, NOW + 905, store, cfg)
    assert basket.status == "CLOSED" and basket.close_reason == "timeout"


def test_broker_side_stop_hit_is_detected_and_counts_as_a_loss():
    cfg, b, store, basket = open_one(positions=1)
    t = basket.positions[0].ticket
    b.broker_closes(t, -basket.planned_risk_money)
    ex.manage_basket(basket, b, MPARAMS, NOW + 40, store, cfg)
    assert basket.status == "CLOSED" and basket.close_reason == "stop_or_target"
    assert store.counters.losses == 1 and store.counters.consecutive_losses == 1


def test_two_losing_baskets_lock_out_new_trading():
    store = BasketStore()
    for i in range(2):
        b = FakeBroker(XAU)
        c = ctx(broker=b, store=store, c=cand(key=f"FVG:{i}"))
        c.store = store
        g = ex.run_gate(c)
        assert g.ok, g.reason
        basket = ex.execute_basket(c, g).basket
        b.broker_closes(basket.positions[0].ticket, -basket.planned_risk_money)
        ex.manage_basket(basket, b, MPARAMS, NOW + 10, store, c.cfg)
    assert store.counters.locked_out
    assert ex.run_gate(ctx(store=store, c=cand(key="FVG:99"))).reason == "locked_out"


# ---------------------------------------------------------------- recovery
def test_recovery_after_restart_syncs_closed_positions(tmp_path):
    path = str(tmp_path / "s.json")
    cfg = cfg_of()
    b = FakeBroker(XAU)
    store = BasketStore(path)
    c = ctx(cfg=cfg, broker=b, store=store)
    basket = ex.execute_basket(c, ex.run_gate(c)).basket
    b.broker_closes(basket.positions[0].ticket, -7.5)                  # closed while the bot was down
    fresh = BasketStore.load(path)
    assert len(fresh.open_baskets()) == 1
    ex.recover(fresh, b, cfg)
    assert not fresh.open_baskets() and fresh.counters.realized_net == pytest.approx(-7.5)
    assert fresh.counters.trades_opened == 1


def test_recovery_flags_unknown_positions_without_managing_them(tmp_path):
    cfg = cfg_of()
    b = FakeBroker(XAU)
    store = BasketStore()
    b.send_market("XAUUSD", 1, 0.01, 1990.0, 2010.0, "manual trade", 20)
    b.send_market("XAUUSD", 1, 0.01, 1990.0, 2010.0, "TB|ghost-1|0", 20)
    ex.recover(store, b, cfg)
    assert [e["type"] for e in store.events].count("UNKNOWN_POSITION") == 2
    assert not store.open_baskets()


def test_corrupt_state_file_locks_trading(tmp_path):
    p = tmp_path / "s.json"
    p.write_text("{broken")
    s = BasketStore.load(str(p))
    assert s.counters.locked_out and s.events[0]["type"] == "STATE_CORRUPT"


def test_state_roundtrip(tmp_path):
    path = str(tmp_path / "s.json")
    cfg, b, store, basket = open_one()
    store.path = path
    store.save()
    loaded = BasketStore.load(path)
    lb = loaded.baskets[basket.id]
    assert lb.total_lots == basket.total_lots and lb.spec == basket.spec and lb.window_seconds == basket.window_seconds
    assert loaded.counters.trades_opened == 1


# ---------------------------------------------------------------- helpers
def test_volatility_state_and_schedule():
    cfg = cfg_of()
    assert ex.volatility_state(1.0, 1.0, cfg) == "normal"
    assert ex.volatility_state(1.6, 1.0, cfg) == "elevated"
    assert ex.volatility_state(2.6, 1.0, cfg) == "extreme"
    assert ex.volatility_state(0.0, 1.0, cfg) == "unknown"
    assert ex.schedule_allows(DAY + 9 * 3600, cfg)               # 10:00 WAT
    assert not ex.schedule_allows(DAY + 20 * 3600, cfg)          # 21:00 WAT = cutoff
    assert ex.schedule_allows(DAY + 19 * 3600 + 3540, cfg)       # 20:59 WAT


def test_prop_profile_is_enforced_in_the_gate():
    prof = PropProfile(name="F", confirmed=True, starting_balance=1000, daily_loss_limit_percent=5,
                       max_drawdown_percent=10, safety_buffer_percent=0.5)
    cfg = cfg_of(prop_profiles={"F": prof}, active_prop_profile="F")
    assert ex.run_gate(ctx(cfg=cfg)).reason == "prop_state_unavailable"
    ok_state = PropState(balance=1000, equity=1000, day_start_balance=1000, high_water=1000)
    assert ex.run_gate(ctx(cfg=cfg, prop=ok_state)).ok
    hurt = PropState(balance=960, equity=955, day_start_balance=1000, high_water=1000)    # 4.5% down today
    assert ex.run_gate(ctx(cfg=cfg, prop=hurt)).reason == "prop_daily_loss"


def test_account_allowlist():
    other = FakeBroker(XAU, login=999999)
    g = ex.run_gate(ctx(broker=other))
    assert not g.ok and g.reason == "account_not_allowed" and not g.send_orders
    none_listed = cfg_of(broker=BrokerConfig())                      # empty allowlist: no trading in DEMO/LIVE
    assert ex.run_gate(ctx(cfg=none_listed)).reason == "account_not_allowed"
    sig = cfg_of(Mode.SIGNAL_ONLY, broker=BrokerConfig())            # signal-only never trades, so no account needed
    assert ex.run_gate(ctx(cfg=sig, instrument_state=InstrumentState.MONITORED)).reason == "signal_only"
    assert ex.run_gate(ctx()).ok


def test_allowed_logins_validation():
    import pytest as _pt
    from pydantic import ValidationError
    with _pt.raises(ValidationError):
        BrokerConfig(allowed_logins=[0])
    with _pt.raises(ValidationError):
        BrokerConfig(allowed_logins=[1, 1])
    with _pt.raises(ValidationError):
        BrokerConfig(allowed_logins=[1, 2, 3, 4, 5, 6])
    assert BrokerConfig(allowed_logins=[12345678]).allowed_logins == [12345678]


# ---------------------------------------------------------------- news / anomaly / portfolio inside the gate
def test_gate_news_extras_and_anomaly():
    assert ex.run_gate(ctx(market_anomaly=True)).reason == "market_anomaly"
    assert ex.run_gate(ctx(news_extra_vol=True, volatility="elevated")).reason == "news_extra_volatility"
    assert ex.run_gate(ctx(news_extra_vol=True, volatility="normal")).ok
    # spread 0.2 on a 5.2 stop: normal limit 0.78 passes, halved limit 0.39 still passes; tighten with a wider spread
    wide = FakeBroker(XAU, bid=2000.0, ask=2000.6)
    c = cand(entry=2000.6)
    assert ex.run_gate(ctx(broker=wide, c=c)).ok
    assert ex.run_gate(ctx(broker=wide, c=c, news_extra_spread=True)).reason == "news_extra_spread"


def test_gate_blocks_shared_exposure_with_open_basket():
    cfg = cfg_of(risk=RiskConfig(risk_percent=1.0, max_open_risk_percent=5.0, max_simultaneous_positions=4))
    store = BasketStore()
    b = FakeBroker(XAU)
    first = ex.execute_basket(ctx(cfg=cfg, broker=b, store=store), ex.run_gate(ctx(cfg=cfg, broker=b, store=store)))
    assert first.ok
    eur = SymbolSpec("EURUSD", 0.00001, 5, 0.00001, 1.0, 1.0, 0.01, 200.0, 0.01, margin_per_lot=500.0)

    class Two(FakeBroker):
        def spec(self, symbol):
            return {"XAUUSD": XAU, "EURUSD": eur}.get(symbol)
    b2 = Two(eur, bid=1.10000, ask=1.10002)
    cand_eur = Candidate(1, 1.10002, 1.0950, "FVG", NOW - 5, "FVG:eur")
    g = ex.run_gate(GateContext(cfg=cfg, now=NOW, config_age_seconds=1.0, symbol="EURUSD",
                                instrument_state=InstrumentState.DEMO_ELIGIBLE, cand=cand_eur, broker=b2,
                                store=store, news_status="clear", volatility="normal"))
    assert g.reason == "shared_currency_exposure"      # long gold and long EURUSD are both short USD
