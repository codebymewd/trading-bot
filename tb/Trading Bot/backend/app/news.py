"""Selective news handling.

The bot neither avoids every release nor trades every release. Each scheduled event is classified by impact
and the affected currencies; the NewsConfig policy for that impact decides whether entries are allowed, which
extra checks apply (spread/volatility), and the pre/post windows. Missing, stale or incomplete calendar data is
NOT treated as "no news": it follows `on_data_unavailable` (default: block). Direction always comes from the
strategy's structure, never from a predicted outcome. Unscheduled shocks are only approximated by price/spread
anomaly detection and can not be detected reliably.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set

from .config_schema import NewsConfig, PropProfile

IMPACTS = ("low", "medium", "high")
FOREX_CODES = {"USD", "EUR", "GBP", "JPY", "CHF", "AUD", "NZD", "CAD", "CNH", "SEK", "NOK", "MXN", "ZAR", "TRY", "SGD", "HKD", "PLN"}
METAL_CODES = {"XAU", "XAG", "XPT", "XPD"}
CRYPTO_CODES = {"BTC", "ETH", "LTC", "XRP", "BCH", "SOL", "ADA", "DOT", "DOGE", "BNB"}


@dataclass(frozen=True)
class NewsEvent:
    time: int                 # UTC epoch seconds of the scheduled release
    currency: str
    impact: str               # low | medium | high
    title: str = ""
    kind: str = ""            # e.g. "NFP", "CPI", "RATE" - used for permitted high-impact types
    forecast: Optional[float] = None
    actual: Optional[float] = None


@dataclass
class NewsCalendar:
    events: List[NewsEvent] = field(default_factory=list)
    fetched_at: int = 0           # when the data was obtained
    covers_until: int = 0         # the calendar is complete up to this time

    @classmethod
    def from_json(cls, text: str) -> "NewsCalendar":
        d = json.loads(text)
        ev = [NewsEvent(int(e["time"]), str(e["currency"]).upper(), str(e["impact"]).lower(), e.get("title", ""),
                        e.get("kind", ""), e.get("forecast"), e.get("actual")) for e in d["events"]]
        for e in ev:
            if e.impact not in IMPACTS:
                raise ValueError(f"bad impact {e.impact!r}")
        return cls(ev, int(d["fetched_at"]), int(d["covers_until"]))


@dataclass
class NewsDecision:
    status: str                   # clear | restricted | unavailable
    reason: str = ""
    event: Optional[NewsEvent] = None
    extra_spread_check: bool = False
    extra_volatility_check: bool = False


def currencies_for_symbol(symbol: str, overrides: Optional[Dict[str, List[str]]] = None) -> List[str]:
    """Currencies/factors that news can move for this symbol. [] = unknown mapping (cannot assess)."""
    key = symbol.upper()
    if overrides and key in overrides:
        return [c.upper() for c in overrides[key]]
    letters = "".join(ch for ch in key if ch.isalpha())          # drops broker suffix digits/underscores
    head = letters[:6]
    if len(head) >= 6 and head[:3] in FOREX_CODES and head[3:6] in FOREX_CODES:
        return [head[:3], head[3:6]]
    if len(head) >= 3 and head[:3] in METAL_CODES | CRYPTO_CODES:
        return ["USD"]                                           # priced in USD; USD releases move them
    return []


def asset_class_of(symbol: str) -> str:
    letters = "".join(ch for ch in symbol.upper() if ch.isalpha())
    head = letters[:6]
    if len(head) >= 6 and head[:3] in FOREX_CODES and head[3:6] in FOREX_CODES:
        return "forex"
    if head[:3] in METAL_CODES:
        return "metals"
    if head[:3] in CRYPTO_CODES:
        return "crypto"
    return "other"


def _policy(cfg: NewsConfig, impact: str):
    return {"low": cfg.low, "medium": cfg.medium, "high": cfg.high}[impact]


def news_status(now: int, symbol: str, cal: Optional[NewsCalendar], cfg: NewsConfig,
                prop: Optional[PropProfile] = None, overrides: Optional[Dict[str, List[str]]] = None) -> NewsDecision:
    currencies = currencies_for_symbol(symbol, overrides)
    if not currencies:
        return NewsDecision("unavailable", "unknown_currency_mapping")
    if cal is None:
        return NewsDecision("unavailable", "no_calendar")
    if now - cal.fetched_at > cfg.data_stale_after_minutes * 60:
        return NewsDecision("unavailable", "calendar_stale")
    horizon = max(cfg.low.pre_window_min, cfg.medium.pre_window_min, cfg.high.pre_window_min,
                  prop.news_window_min if prop and prop.news_restricted else 0) * 60
    if cal.covers_until < now + horizon:
        return NewsDecision("unavailable", "calendar_incomplete")
    extra_spread = extra_vol = False
    worst: Optional[NewsDecision] = None
    for e in cal.events:
        if e.currency not in currencies:
            continue
        pol = _policy(cfg, e.impact)
        pre, post = pol.pre_window_min * 60, pol.post_window_min * 60
        if prop is not None and prop.news_restricted and e.impact == "high":
            pre, post = max(pre, prop.news_window_min * 60), max(post, prop.news_window_min * 60)
        if not (e.time - pre <= now <= e.time + post):
            continue
        prop_blocks = prop is not None and prop.news_restricted and e.impact == "high"
        permitted = e.impact == "high" and e.kind and e.kind in cfg.permitted_high_impact_event_types
        if prop_blocks or not (pol.allow_entries or permitted):
            return NewsDecision("restricted", f"{e.impact}_impact_{e.title or e.currency}", e)
        extra_spread = extra_spread or pol.extra_spread_check or bool(permitted)
        extra_vol = extra_vol or pol.extra_volatility_check or bool(permitted)
        worst = worst or NewsDecision("clear", f"{e.impact}_news_window", e)
    d = worst or NewsDecision("clear")
    d.extra_spread_check, d.extra_volatility_check = extra_spread, extra_vol
    return d


def should_close_before_news(now: int, symbols: Iterable[str], cal: Optional[NewsCalendar], cfg: NewsConfig,
                             prop: Optional[PropProfile] = None) -> bool:
    """True when an open position should be closed because a high-impact release is inside its pre-window and the
    configuration or the prop firm requires flat positions."""
    if cal is None:
        return False
    must = cfg.close_before_high_impact or (prop is not None and prop.news_restricted)
    if not must:
        return False
    pre = max(cfg.high.pre_window_min, prop.news_window_min if prop and prop.news_restricted else 0) * 60
    cur: Set[str] = set()
    for s in symbols:
        cur.update(currencies_for_symbol(s))
    return any(e.impact == "high" and e.currency in cur and e.time - pre <= now <= e.time for e in cal.events)


def market_anomaly(spread_now: float, spread_median: float, last_bar_range: float, atr: float,
                   spread_mult: float = 3.0, range_mult: float = 3.0) -> bool:
    """Crude guard for unscheduled shocks: sudden spread blow-out or an outsized bar. Cannot catch everything."""
    if spread_median > 0 and spread_now > spread_mult * spread_median:
        return True
    if atr > 0 and last_bar_range > range_mult * atr:
        return True
    return False


def surprise(event: NewsEvent) -> Optional[float]:
    """Actual minus forecast, when both are known. Informational only: it is never used to pick a direction."""
    if event.actual is None or event.forecast is None:
        return None
    return event.actual - event.forecast
