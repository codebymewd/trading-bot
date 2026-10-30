import json
import os
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from backend.app.config_schema import DailyLimits, ScheduleConfig, SystemConfig
from backend.app.trading_day import (DailyCounters, TimezoneError, load_counters, resolve_fixed_offset,
                                     save_counters, trading_day_id, trading_day_label,
                                     trading_day_window_utc)

CONTRACT = json.load(open(os.path.join(os.path.dirname(__file__), "..", "config", "contract.json")))
LAGOS = 3600


def utc(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def test_lagos_is_fixed_plus_one_hour():
    assert resolve_fixed_offset("Africa/Lagos") == LAGOS


def test_defaults_use_lagos_midnight():
    c = SystemConfig()
    assert c.daily.reset_timezone == "Africa/Lagos" and c.daily.reset_time == "00:00"
    assert c.ea_payload()["derived"]["reset_utc_offset_seconds"] == LAGOS


@pytest.mark.parametrize("bad", ["Mars/Base", "WAT", "", "Africa/Nowhere", "America/New_York", "Europe/London"])
def test_bad_or_dst_timezones_rejected(bad):
    with pytest.raises(Exception):
        DailyLimits(reset_timezone=bad)
    with pytest.raises(Exception):
        ScheduleConfig(timezone=bad)


def test_broker_server_allowed():
    assert DailyLimits(reset_timezone="broker_server").reset_timezone == "broker_server"


@pytest.mark.parametrize("v", CONTRACT["trading_day_vectors"], ids=lambda v: v["name"])
def test_golden_vectors(v):
    expected = (date.fromisoformat(v["label"]) - date(1970, 1, 1)).days
    got = trading_day_id(utc(v["utc"]), v["offset"], v["reset"])
    assert got == expected
    assert trading_day_label(got) == v["label"]


def test_nigerian_midnight_not_utc_midnight():
    before = trading_day_id(utc("2026-10-19T22:59:59Z"), LAGOS)
    at = trading_day_id(utc("2026-10-19T23:00:00Z"), LAGOS)
    assert at == before + 1
    # UTC midnight is 01:00 WAT: no roll there
    assert trading_day_id(utc("2026-10-19T23:59:59Z"), LAGOS) == at
    assert trading_day_id(utc("2026-10-20T00:00:00Z"), LAGOS) == at


def test_integer_formula_matches_zoneinfo_calendar_day():
    """EA uses the integer formula; verify it equals the real Lagos calendar date."""
    zone = ZoneInfo("Africa/Lagos")
    t = utc("2026-01-01T00:00:00Z")
    for _ in range(24 * 60 * 40):  # 40 days, minute resolution
        label = trading_day_label(trading_day_id(t, LAGOS))
        assert label == t.astimezone(zone).strftime("%Y-%m-%d")
        t += timedelta(minutes=1)


def test_daily_window_boundaries():
    start, end = trading_day_window_utc(utc("2026-10-19T12:00:00Z"), LAGOS)
    assert start == utc("2026-10-18T23:00:00Z") and end == utc("2026-10-19T23:00:00Z")


def _loaded_counters(at="2026-10-19T09:00:00Z"):
    c = DailyCounters()
    c.roll(utc(at), LAGOS)
    c.trades_opened, c.losses, c.consecutive_losses, c.locked_out = 2, 2, 2, True
    return c


def test_restart_same_day_keeps_lockout(tmp_path):
    path = str(tmp_path / "daily.json")
    save_counters(path, _loaded_counters())
    restarted = load_counters(path)
    assert restarted.roll(utc("2026-10-19T22:59:00Z"), LAGOS) is False  # 23:59 WAT
    assert restarted.locked_out and restarted.trades_opened == 2 and restarted.losses == 2


def test_utc_midnight_restart_does_not_reset(tmp_path):
    path = str(tmp_path / "daily.json")
    # state written at 00:30 WAT on 20 Oct (23:30 UTC on 19 Oct); UTC midnight passes after that
    save_counters(path, _loaded_counters("2026-10-19T23:30:00Z"))
    restarted = load_counters(path)
    assert restarted.roll(utc("2026-10-20T00:30:00Z"), LAGOS) is False  # 01:30 WAT, same trading day
    assert restarted.locked_out


def test_nigerian_midnight_resets_daily_but_not_consecutive_losses():
    c = _loaded_counters()
    assert c.roll(utc("2026-10-19T23:00:00Z"), LAGOS) is True
    assert c.trades_opened == 0 and c.losses == 0 and not c.locked_out and c.realized_net == 0.0
    assert c.consecutive_losses == 2


def test_clock_moving_backwards_never_resets():
    c = _loaded_counters()
    assert c.roll(utc("2026-10-18T12:00:00Z"), LAGOS) is False
    assert c.clock_anomaly and c.locked_out and c.trades_opened == 2


def test_corrupt_state_file_fails_safe(tmp_path):
    path = tmp_path / "daily.json"
    path.write_text("{not json")
    c = load_counters(str(path))
    assert c.locked_out


def test_missing_state_file_is_fresh(tmp_path):
    c = load_counters(str(tmp_path / "none.json"))
    assert not c.locked_out and c.trading_day == -1
