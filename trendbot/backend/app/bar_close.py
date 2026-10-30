"""Closed-candle discipline: bar identity + once-only evaluation (the candle-close bug fix).

Problem
-------
The live path was supposed to act on CLOSED candles only, but nothing enforced it: every tick
re-evaluated the same forming bar, so one setup could be evaluated many times and retried after
EA restarts / backend reconnects / heartbeat retries.

Fix
---
1. A bar is identified by (symbol, timeframe, open_time). Nothing else. Timestamps of closed bars
   are stable across ticks, restarts and reconnections, so this identity survives all of them.
2. `is_closed()` requires a *confirmed* next-bar open time strictly after this bar's open time.
   A forming candle can never satisfy that; it is never used as confirmed information.
3. `BarGate.evaluate_once()` runs the callback for a bar at most once per process AND persists the
   last processed bar-open time per (symbol, timeframe) in the database, so duplicates cannot
   happen after a backend restart either. The executor keeps its own durable `used_setups`
   (setup_id includes the M5 close time) which blocks EA-side duplicates after EA restart/retry.

The identical rules are implemented in MQL5 (ea/Include/TrendBotTrader.mqh, TB_IsClosed / new-bar
gate) - keep both sides behaviour-identical.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, Optional, Set, Tuple


@dataclass(frozen=True)
class BarIdentity:
    symbol: str
    timeframe: str          # "M5", "M15", "H4", "D1"
    open_time: int          # epoch seconds of the bar OPEN (iTime(_Symbol, tf, shift))

    def key(self) -> str:
        return f"{self.symbol}|{self.timeframe}|{self.open_time}"


@dataclass
class BarSnapshot:
    """One historical bar as reported by the data source (EA or backtester)."""
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    next_open_time: Optional[int] = None   # iTime(...,1) when reading shift 0; None if unknown


def is_closed(bar: BarSnapshot, current_server_time: Optional[int] = None,
              tf_seconds: Optional[int] = None) -> bool:
    """A bar is CLOSED only when we can prove the following bar exists.

    Proof order (strongest first):
      1. next_open_time is known and > open_time (a newer bar arrived => this one is final);
      2. server time has passed open_time + tf_seconds (fallback when history is shifted;
         still conservative: equal boundary counts as NOT closed).
    Anything else (forming candle, missing data) returns False - never trade on it.
    """
    if bar.next_open_time is not None:
        return int(bar.next_open_time) > int(bar.open_time)
    if current_server_time is not None and tf_seconds:
        return int(current_server_time) >= int(bar.open_time) + int(tf_seconds)
    return False


class BarGate:
    """Once-and-only-once evaluation gate keyed by BarIdentity.

    - Multiple ticks on the same bar: callback runs only for the first call (per process).
    - Backend restart: the persisted last-processed open_time per (symbol, tf) blocks re-running
      an already-consumed bar even though the in-memory set is empty.
    - Older bars (history replay) are ignored: only bars NEWER than the last processed one run.
    """

    def __init__(self, db=None, clock: Callable[[], float] = None):
        self.db = db
        self._seen: Set[Tuple[str, str]] = set()          # (symbol|tf, open_time) pairs this process handled
        self._last: Dict[Tuple[str, str], int] = {}       # (symbol, tf) -> highest processed open_time
        if db is not None:
            for sym_tf, t in (db.kv_get("bar_gate") or {}).items():
                self._last[tuple(sym_tf.split("|", 1))] = int(t)

    def _persist(self) -> None:
        if self.db is not None:
            self.db.kv_set("bar_gate", {f"{s}|{tf}": t for (s, tf), t in self._last.items()})

    def already_processed(self, ident: BarIdentity) -> bool:
        k = (ident.symbol, ident.timeframe)
        return ident.key() in self._seen or self._last.get(k, -1) >= ident.open_time

    def evaluate_once(self, ident: BarIdentity, fn: Callable[[], None]) -> bool:
        """Run fn() exactly once for this bar identity. Returns True if fn ran."""
        if self.already_processed(ident):
            return False
        fn()
        k = (ident.symbol, ident.timeframe)
        self._seen.add(ident.key())
        self._last[k] = max(self._last.get(k, -1), ident.open_time)
        self._persist()
        return True

    def mark_processed(self, ident: BarIdentity) -> None:
        """Mark a bar consumed without running anything (e.g. signal rejected - do not retry it)."""
        k = (ident.symbol, ident.timeframe)
        self._seen.add(ident.key())
        self._last[k] = max(self._last.get(k, -1), ident.open_time)
        self._persist()


TF_SECONDS = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800, "H1": 3600, "H4": 14400, "D1": 86400}


def closed_history(symbol: str, timeframe: str, bars, current_server_time: Optional[int] = None):
    """Filter a list of BarSnapshot (ascending) down to the CLOSED ones.

    The newest bar is treated as FORMING unless its closure is proven (next_open_time present or
    server time past its end). This is what the strategy engines must consume: never a forming bar.
    """
    tf_s = TF_SECONDS.get(timeframe)
    out = []
    for b in bars:
        if is_closed(b, current_server_time, tf_s):
            out.append(b)
    return out


def make_identity(symbol: str, timeframe: str, bar: BarSnapshot) -> BarIdentity:
    return BarIdentity(symbol, timeframe, int(bar.open_time))
