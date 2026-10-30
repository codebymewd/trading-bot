import json
import random

import pytest

from backend.app import news as nw
from backend.app.config_schema import NewsConfig, NewsImpactPolicy, PropProfile
from backend.app.news import NewsCalendar, NewsEvent

NOW = 1_700_000_000
H = 3600


def cal(events, fetched=NOW - 60, covers=NOW + 6 * H):
    return NewsCalendar(events, fetched, covers)


def ev(offset_min, cur="USD", impact="high", kind="NFP", title="x"):
    return NewsEvent(NOW + offset_min * 60, cur, impact, title, kind)


CFG = NewsConfig()   # low: allowed; medium: allowed + extra checks, 5/5 min; high: blocked 30 before / 15 after


@pytest.mark.parametrize("symbol,expected", [
    ("EURUSD", ["EUR", "USD"]), ("GBPUSDm", ["GBP", "USD"]), ("usdjpy", ["USD", "JPY"]),
    ("XAUUSDm", ["USD"]), ("BTCUSD", ["USD"]), ("XAUUSD.pro", ["USD"]), ("US30", []), ("FOOBAR", []),
])
def test_currency_mapping(symbol, expected):
    assert nw.currencies_for_symbol(symbol) == expected


def test_currency_mapping_override():
    assert nw.currencies_for_symbol("US30m", {"US30M": ["USD"]}) == ["USD"]


def test_unknown_mapping_cannot_be_assessed():
    d = nw.news_status(NOW, "US30", cal([]), CFG)
    assert d.status == "unavailable" and d.reason == "unknown_currency_mapping"


def test_missing_stale_or_incomplete_calendar_is_unavailable_not_clear():
    assert nw.news_status(NOW, "EURUSD", None, CFG).reason == "no_calendar"
    assert nw.news_status(NOW, "EURUSD", cal([], fetched=NOW - 4 * H), CFG).reason == "calendar_stale"
    assert nw.news_status(NOW, "EURUSD", cal([], covers=NOW + 10 * 60), CFG).reason == "calendar_incomplete"
    assert nw.news_status(NOW, "EURUSD", cal([]), CFG).status == "clear"


def test_high_impact_blocks_inside_window_only():
    c = cal([ev(20)])                                    # release in 20 minutes, pre-window is 30
    d = nw.news_status(NOW, "XAUUSDm", c, CFG)
    assert d.status == "restricted" and d.event.title == "x"
    assert nw.news_status(NOW, "XAUUSDm", cal([ev(45)]), CFG).status == "clear"    # still outside the pre-window
    assert nw.news_status(NOW, "XAUUSDm", cal([ev(-14)]), CFG).status == "restricted"
    assert nw.news_status(NOW, "XAUUSDm", cal([ev(-16)]), CFG).status == "clear"   # post-window over


def test_other_currencies_do_not_matter():
    assert nw.news_status(NOW, "EURUSD", cal([ev(10, cur="JPY")]), CFG).status == "clear"
    assert nw.news_status(NOW, "EURUSD", cal([ev(10, cur="EUR")]), CFG).status == "restricted"   # base currency counts


def test_medium_impact_allows_with_extra_checks_and_low_is_free():
    d = nw.news_status(NOW, "EURUSD", cal([ev(3, impact="medium", kind="")]), CFG)
    assert d.status == "clear" and d.extra_spread_check and d.extra_volatility_check
    d = nw.news_status(NOW, "EURUSD", cal([ev(1, impact="low", kind="")]), CFG)
    assert d.status == "clear" and not d.extra_spread_check


def test_permitted_high_impact_event_types():
    cfg = NewsConfig(permitted_high_impact_event_types=["CPI"])
    assert nw.news_status(NOW, "EURUSD", cal([ev(10, kind="CPI")]), cfg).status == "clear"
    d = nw.news_status(NOW, "EURUSD", cal([ev(10, kind="CPI")]), cfg)
    assert d.extra_spread_check and d.extra_volatility_check          # permitted, but with the stricter checks
    assert nw.news_status(NOW, "EURUSD", cal([ev(10, kind="NFP")]), cfg).status == "restricted"


def test_policy_is_configurable():
    open_high = NewsConfig(high=NewsImpactPolicy(allow_entries=True))
    assert nw.news_status(NOW, "EURUSD", cal([ev(5)]), open_high).status == "clear"
    strict_medium = NewsConfig(medium=NewsImpactPolicy(allow_entries=False, pre_window_min=10, post_window_min=10))
    assert nw.news_status(NOW, "EURUSD", cal([ev(5, impact="medium", kind="")]), strict_medium).status == "restricted"


def test_prop_firm_news_rule_overrides_permissions():
    prop = PropProfile(name="F", confirmed=True, starting_balance=1000, news_restricted=True, news_window_min=5)
    cfg = NewsConfig(high=NewsImpactPolicy(allow_entries=True), permitted_high_impact_event_types=["NFP"])
    assert nw.news_status(NOW, "EURUSD", cal([ev(3)]), cfg, prop).status == "restricted"
    assert nw.news_status(NOW, "EURUSD", cal([ev(3)]), cfg, None).status == "clear"


def test_close_before_news():
    c = cal([ev(10)])
    assert not nw.should_close_before_news(NOW, ["EURUSD"], c, CFG)                       # holding through news is allowed
    cfg = NewsConfig(close_before_high_impact=True)
    assert nw.should_close_before_news(NOW, ["EURUSD"], c, cfg)
    assert not nw.should_close_before_news(NOW, ["USDJPY"], cal([ev(10, cur="GBP")]), cfg)
    prop = PropProfile(name="F", confirmed=True, starting_balance=1000, news_restricted=True, news_window_min=15)
    assert nw.should_close_before_news(NOW, ["EURUSD"], c, CFG, prop)


def test_anomaly_detection_and_surprise_is_informational():
    assert nw.market_anomaly(0.9, 0.2, 1.0, 1.0)             # spread 4.5x normal
    assert nw.market_anomaly(0.2, 0.2, 3.5, 1.0)             # bar 3.5x ATR
    assert not nw.market_anomaly(0.3, 0.2, 1.5, 1.0)
    e = NewsEvent(NOW, "USD", "high", "CPI", "CPI", forecast=3.0, actual=3.4)
    assert nw.surprise(e) == pytest.approx(0.4)
    assert nw.surprise(NewsEvent(NOW, "USD", "high")) is None


def test_calendar_json_roundtrip_and_validation():
    text = json.dumps({"fetched_at": NOW, "covers_until": NOW + 86400,
                       "events": [{"time": NOW + 60, "currency": "usd", "impact": "HIGH", "title": "NFP", "kind": "NFP"}]})
    c = NewsCalendar.from_json(text)
    assert c.events[0].currency == "USD" and c.events[0].impact == "high"
    bad = text.replace("HIGH", "huge")
    with pytest.raises(ValueError):
        NewsCalendar.from_json(bad)
