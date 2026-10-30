"""Trading-day definition and restart-safe daily counters.

Definition (identical in the EA, see ea/Include/TrendBotCore.mqh):

    local_seconds = utc_epoch + utc_offset_seconds - reset_minutes * 60
    trading_day_id = floor(local_seconds / 86400)          # days since 1970-01-01

For Africa/Lagos (WAT, UTC+1, no DST) with reset 00:00 the day rolls at
23:00 UTC, i.e. at Nigerian midnight - NOT at UTC midnight.

The EA has no timezone database, so it only supports zones whose UTC offset is
constant (no DST). `resolve_fixed_offset` enforces this; zones with DST are rejected
at validation time rather than silently mishandled.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

BROKER_SERVER = "broker_server"
_CHECK_YEARS = range(2020, 2036)


class TimezoneError(ValueError):
    pass


def parse_hhmm(value: str) -> int:
    """'HH:MM' -> minutes after midnight. Raises ValueError on bad input."""
    h, m = value.split(":")
    h, m = int(h), int(m)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"invalid time {value!r}")
    return h * 60 + m


def resolve_fixed_offset(tz_name: str) -> Optional[int]:
    """UTC offset in seconds for a constant-offset IANA zone.

    Returns None for 'broker_server' (offset only known at runtime).
    Raises TimezoneError for unknown names, or zones whose offset changes
    (DST) between 2020 and 2035, because the EA cannot follow those yet.
    """
    if tz_name == BROKER_SERVER:
        return None
    try:
        zone = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise TimezoneError(
            f"unknown timezone {tz_name!r} (on Windows run: pip install tzdata)"
        ) from exc
    offsets = set()
    for year in _CHECK_YEARS:
        for month in (1, 4, 7, 10):
            offsets.add(datetime(year, month, 15, 12, tzinfo=timezone.utc).astimezone(zone).utcoffset())
    if len(offsets) != 1:
        raise TimezoneError(f"timezone {tz_name!r} observes DST; EA supports constant-offset zones only")
    return int(offsets.pop().total_seconds())


def trading_day_id(now_utc: datetime, utc_offset_seconds: int, reset_time: str = "00:00") -> int:
    """Integer trading-day id. Same formula as the EA (pure integer arithmetic)."""
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be timezone-aware")
    epoch = int(now_utc.timestamp())
    return (epoch + utc_offset_seconds - parse_hhmm(reset_time) * 60) // 86400


def trading_day_label(day_id: int) -> str:
    """Human label of the trading day (its calendar date in the reset timezone)."""
    return (datetime(1970, 1, 1) + timedelta(days=day_id)).strftime("%Y-%m-%d")


def trading_day_window_utc(now_utc: datetime, utc_offset_seconds: int, reset_time: str = "00:00"):
    """[start, end) of the current trading day as UTC datetimes (for daily-loss / deal queries)."""
    day = trading_day_id(now_utc, utc_offset_seconds, reset_time)
    start_epoch = day * 86400 - utc_offset_seconds + parse_hhmm(reset_time) * 60
    start = datetime.fromtimestamp(start_epoch, tz=timezone.utc)
    return start, start + timedelta(days=1)


@dataclass
class DailyCounters:
    """Persisted daily risk state. Resets ONLY when the trading day advances."""
    trading_day: int = -1
    trades_opened: int = 0
    losses: int = 0
    consecutive_losses: int = 0
    realized_net: float = 0.0
    locked_out: bool = False
    clock_anomaly: bool = False

    # ---- basket accounting (one basket = one "trade" for the daily limits)
    def can_open_basket(self, max_per_day: int) -> bool:
        return not (self.locked_out or self.clock_anomaly or self.trades_opened >= max_per_day)

    def on_basket_opened(self) -> None:
        self.trades_opened += 1

    def on_basket_closed(self, net: float, tolerance: float, lockout_after_losses: int,
                         max_consecutive: Optional[int] = None,
                         daily_loss_limit_money: Optional[float] = None) -> str:
        """Record the NET result (all deals, costs included). Returns WIN | LOSS | NEUTRAL.
        Neutral (|net| <= tolerance) neither adds a loss nor resets the consecutive-loss count."""
        self.realized_net += net
        if net < -tolerance:
            outcome = "LOSS"
            self.losses += 1
            self.consecutive_losses += 1
        elif net > tolerance:
            outcome = "WIN"
            self.consecutive_losses = 0
        else:
            outcome = "NEUTRAL"
        if self.losses >= lockout_after_losses:
            self.locked_out = True
        if max_consecutive is not None and self.consecutive_losses >= max_consecutive:
            self.locked_out = True
        if daily_loss_limit_money is not None and self.realized_net <= -daily_loss_limit_money:
            self.locked_out = True
        return outcome

    def roll(self, now_utc: datetime, utc_offset_seconds: int, reset_time: str = "00:00") -> bool:
        """Advance to the current trading day. Returns True if counters were reset.

        - same day  -> keep everything (restart-safe)
        - later day -> reset daily counters (consecutive_losses is kept: it is not a daily value)
        - earlier day (clock moved backwards) -> keep state, set clock_anomaly, never reset
        """
        today = trading_day_id(now_utc, utc_offset_seconds, reset_time)
        if self.trading_day == -1:
            self.trading_day = today
            return False
        if today == self.trading_day:
            self.clock_anomaly = False
            return False
        if today < self.trading_day:
            self.clock_anomaly = True
            return False
        self.trading_day = today
        self.trades_opened = 0
        self.losses = 0
        self.realized_net = 0.0
        self.locked_out = False
        self.clock_anomaly = False
        return True


def save_counters(path: str, counters: DailyCounters) -> None:
    """Atomic write so a crash cannot leave a half-written state file."""
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(asdict(counters), fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def load_counters(path: str) -> DailyCounters:
    """Missing file -> fresh state. Corrupt file -> locked out (fail safe), never silently reset."""
    if not os.path.exists(path):
        return DailyCounters()
    try:
        with open(path) as fh:
            return DailyCounters(**json.load(fh))
    except Exception:
        return DailyCounters(locked_out=True, clock_anomaly=True)
