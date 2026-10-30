"""Bar-based backtester for the scalp strategy using the SAME sizing, basket rules, daily limits and costs as
the live logic. Honest limitations (also printed in every report):
- Bars only: no tick data, so intrabar order is unknown. If a stop and a target are both inside one bar the STOP is
  assumed to hit first (conservative). Gaps fill at the open.
- Spread is a constant you provide, bars are treated as BID prices, slippage is a constant against us.
- News is only simulated when you supply a calendar; the prop-firm guard and correlation filters are not simulated.
- A good result is not a forecast. Do not tune parameters on the out-of-sample half.
"""
from __future__ import annotations

from bisect import bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from . import basket_manager as bm
from .basket import basket_budget
from .config_schema import SystemConfig
from .executor import schedule_allows
from .news import NewsCalendar, currencies_for_symbol, news_status
from .sizing import SymbolSpec, plan_basket, position_pnl
from .trading_day import DailyCounters, resolve_fixed_offset, trading_day_id

BAR_SECONDS = 300
NEAR_NEWS_SECONDS = 1800


@dataclass
class BtParams:
    start_equity: float = 1000.0
    spread: float = 0.0               # price units
    slippage: float = 0.0             # price units against us on entries and market/stop exits
    be_tolerance_money: float = 0.0   # results within +/- this count as neutral


@dataclass
class Bars:
    tm: List[int]
    o: List[float]
    h: List[float]
    l: List[float]
    c: List[float]
    atr: Optional[List[float]] = None


@dataclass
class TradeResult:
    symbol: str
    direction: int
    kind: str
    open_time: int
    close_time: int
    lots: float
    risk: float
    net: float
    reason: str
    outcome: str
    session: str
    vol_ratio: float
    near_news: bool


@dataclass
class BtResult:
    trades: List[TradeResult] = field(default_factory=list)
    rejected: Counter = field(default_factory=Counter)
    equity_curve: List[Tuple[int, float]] = field(default_factory=list)
    final_equity: float = 0.0


def session_of(tm: int) -> str:
    h = (tm % 86400) // 3600
    return "ASIA" if h < 8 else "LONDON" if h < 13 else "NEWYORK" if h < 21 else "OFF"


def _vol_ratio(bars: Bars, i: int) -> float:
    if not bars.atr or i < 50:
        return 0.0
    win = [x for x in bars.atr[max(0, i - 200):i] if x > 0]
    return bars.atr[i] / (sum(win) / len(win)) if win and bars.atr[i] > 0 else 0.0


def _exit_long(price_bid: float, slip: float) -> float:
    return price_bid - slip


def simulate(bars: Bars, candidates: Sequence, spec: SymbolSpec, cfg: SystemConfig, mgr: bm.ManagerParams,
             bt: BtParams, calendar: Optional[NewsCalendar] = None) -> BtResult:
    """candidates: objects with .time (open time of the M5 signal bar), .dir, .price, .price2 (stop), .reason (kind)."""
    n = len(bars.tm)
    idx = {t: i for i, t in enumerate(bars.tm)}
    res = BtResult()
    equity = bt.start_equity
    res.final_equity = equity
    counters = DailyCounters()
    offset = resolve_fixed_offset(cfg.daily.reset_timezone) or 0
    busy_until = -1

    for cand in candidates:
        if cand.time not in idx:
            continue
        e = idx[cand.time] + 1                           # decision at the signal bar's close -> fill at the next open
        if e >= n:
            continue
        t_entry = bars.tm[e]
        if t_entry <= busy_until:
            res.rejected["basket_open"] += 1
            continue
        counters.roll(_dt(t_entry), offset, cfg.daily.reset_time)
        if counters.locked_out:
            res.rejected["locked_out"] += 1
            continue
        if counters.trades_opened >= cfg.daily.max_trades_per_day:
            res.rejected["daily_basket_limit"] += 1
            continue
        if not schedule_allows(t_entry, cfg):
            res.rejected["outside_trading_window"] += 1
            continue
        d = cand.dir
        if calendar is not None:
            nd = news_status(t_entry, spec.symbol, calendar, cfg.news)
            if nd.status != "clear" and (nd.status == "restricted" or cfg.news.on_data_unavailable == "block"):
                res.rejected["news_" + nd.status] += 1
                continue
        entry = bars.o[e] + bt.spread + bt.slippage if d == 1 else bars.o[e] - bt.slippage
        sl = cand.price2
        risk_dist = abs(cand.price - sl)
        if (entry - cand.price) * d > cfg.entry.max_chase_r * risk_dist:
            res.rejected["price_moved_since_signal"] += 1
            continue
        if bt.spread > cfg.scalp.max_spread_to_stop_ratio * abs(entry - sl):
            res.rejected["spread_too_wide"] += 1
            continue
        budget = equity * cfg.risk.risk_percent / 100.0
        plan = plan_basket(spec, d, entry, sl, budget, cfg.scalp.max_positions_per_basket, cfg.scalp.risk_split,
                           cfg.scalp.target_multiplier, free_margin=equity if spec.margin_per_lot else None)
        if not plan.ok:
            res.rejected["plan_" + plan.reason] += 1
            continue
        b = bm.open_basket(f"BT{t_entry}", spec, plan, [(k + 1, entry) for k in range(len(plan.positions))],
                           t_entry, cfg.scalp.scalp_window_minutes * 60, basket_budget(equity, cfg).max_loss_money)
        counters.on_basket_opened()
        close_time, reason = _run_basket(b, bars, e, spec, mgr, bt)
        net = b.realized
        outcome = counters.on_basket_closed(net, bt.be_tolerance_money, cfg.daily.lockout_after_losses,
                                            cfg.daily.max_consecutive_losses,
                                            equity * cfg.daily.daily_loss_limit_percent / 100.0
                                            if cfg.daily.daily_loss_limit_percent else None)
        equity += net
        busy_until = close_time
        near = False
        if calendar is not None:
            cur = set(currencies_for_symbol(spec.symbol))
            near = any(ev.impact == "high" and ev.currency in cur and abs(ev.time - t_entry) <= NEAR_NEWS_SECONDS
                       for ev in calendar.events)
        res.trades.append(TradeResult(spec.symbol, d, cand.reason, t_entry, close_time, plan.total_lots,
                                      plan.planned_risk_money, net, reason, outcome, session_of(t_entry),
                                      _vol_ratio(bars, e), near))
        res.equity_curve.append((close_time, equity))
    res.final_equity = equity
    return res


def _dt(epoch: int):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, tz=timezone.utc)


def _run_basket(b: bm.Basket, bars: Bars, start: int, spec: SymbolSpec, mgr: bm.ManagerParams, bt: BtParams):
    d = b.direction
    sp, sl_slip = bt.spread, bt.slippage
    for j in range(start, len(bars.tm)):
        o, h, l, c = bars.o[j], bars.h[j], bars.l[j], bars.c[j]
        # 1) intrabar stops first (conservative), then targets
        for pos in list(b.positions):
            if d == 1:
                if l <= pos.sl:
                    px = min(pos.sl, o) - sl_slip
                elif h >= pos.tp > 0:
                    px = max(pos.tp, o)
                else:
                    continue
            else:
                if h + sp >= pos.sl:
                    px = max(pos.sl, o + sp) + sl_slip
                elif l + sp <= pos.tp and pos.tp > 0:
                    px = min(pos.tp, o + sp)
                else:
                    continue
            b.realized += position_pnl(spec, d, pos.entry, px, pos.lots)
            b.positions.remove(pos)
        close_t = bars.tm[j] + BAR_SECONDS
        if not b.positions:
            return close_t, "stop_or_target"
        # 2) manager decisions at the bar close (target/max loss/timeout/break-even)
        bid, ask = c, c + sp
        for a in bm.evaluate(b, close_t, bid, ask, mgr):
            if a.kind == "CLOSE_ALL":
                px = (bid - sl_slip) if d == 1 else (ask + sl_slip)
                for pos in list(b.positions):
                    b.realized += position_pnl(spec, d, pos.entry, px, pos.lots)
                    b.positions.remove(pos)
                return close_t, a.reason
            if a.kind == "PARTIAL_CLOSE":
                px = (bid - sl_slip) if d == 1 else (ask + sl_slip)
                for ticket, lots in a.detail["lots"].items():
                    pos = next(p for p in b.positions if p.ticket == ticket)
                    bm.confirm_partial(b, ticket, lots, position_pnl(spec, d, pos.entry, px, lots))
            elif a.kind == "MODIFY_SL":
                bm.confirm_sl_moved(b, a.detail["sl"], timeout=(a.reason == "timeout_breakeven"))
            elif a.kind == "MARK_TIMEOUT":
                bm.apply_internal(b, [a])
    last = len(bars.tm) - 1                              # data ended with the basket still open: close at the last price
    px = bars.c[last] - sl_slip if d == 1 else bars.c[last] + sp + sl_slip
    for pos in list(b.positions):
        b.realized += position_pnl(spec, d, pos.entry, px, pos.lots)
        b.positions.remove(pos)
    return bars.tm[last] + BAR_SECONDS, "end_of_data"


# ------------------------------------------------------------------ metrics
def _group(trades: Sequence[TradeResult], key) -> Dict[str, dict]:
    g = defaultdict(list)
    for t in trades:
        g[key(t)].append(t)
    return {k: {"trades": len(v), "net": round(sum(t.net for t in v), 2),
                "win_rate": round(sum(1 for t in v if t.outcome == "WIN") / len(v), 3)} for k, v in sorted(g.items())}


def compute_metrics(trades: Sequence[TradeResult], start_equity: float) -> dict:
    n = len(trades)
    wins = [t for t in trades if t.outcome == "WIN"]
    losses = [t for t in trades if t.outcome == "LOSS"]
    gp = sum(t.net for t in trades if t.net > 0)
    gl = -sum(t.net for t in trades if t.net < 0)
    equity = peak = start_equity
    max_dd = 0.0
    for t in trades:
        equity += t.net
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak * 100.0 if peak > 0 else 0.0)
    streak_w = streak_l = best_w = best_l = 0
    for t in trades:
        streak_w = streak_w + 1 if t.outcome == "WIN" else 0
        streak_l = streak_l + 1 if t.outcome == "LOSS" else 0
        best_w, best_l = max(best_w, streak_w), max(best_l, streak_l)
    net = sum(t.net for t in trades)

    def vbucket(t):
        return "n/a" if t.vol_ratio <= 0 else "low(<0.8)" if t.vol_ratio < 0.8 else "normal(0.8-1.5)" if t.vol_ratio < 1.5 else "high(>=1.5)"
    return {
        "trades": n, "wins": len(wins), "losses": len(losses), "neutral": n - len(wins) - len(losses),
        "win_rate": round(len(wins) / n, 3) if n else 0.0,
        "profit_factor": round(gp / gl, 2) if gl > 0 else (float("inf") if gp > 0 else 0.0),
        "net_profit": round(net, 2), "net_return_pct": round(net / start_equity * 100.0, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "avg_win": round(sum(t.net for t in wins) / len(wins), 2) if wins else 0.0,
        "avg_loss": round(sum(t.net for t in losses) / len(losses), 2) if losses else 0.0,
        "expectancy_per_trade": round(net / n, 2) if n else 0.0,
        "max_consecutive_wins": best_w, "max_consecutive_losses": best_l,
        "by_symbol": _group(trades, lambda t: t.symbol),
        "by_session": _group(trades, lambda t: t.session),
        "by_volatility": _group(trades, vbucket),
        "by_news": _group(trades, lambda t: "near_high_impact_news" if t.near_news else "no_news_nearby"),
        "by_exit_reason": _group(trades, lambda t: t.reason),
    }


def split_trades(trades: Sequence[TradeResult], fraction: float = 0.7) -> Tuple[List[TradeResult], List[TradeResult]]:
    """Chronological development / out-of-sample split by trade count."""
    k = int(len(trades) * fraction)
    return list(trades[:k]), list(trades[k:])


def format_report(title: str, m: dict, rejected: Optional[Counter] = None) -> str:
    lines = [f"== {title} ==",
             f"Trades {m['trades']}  (wins {m['wins']}, losses {m['losses']}, neutral {m['neutral']})  win rate {m['win_rate']:.1%}",
             f"Net {m['net_profit']}  ({m['net_return_pct']}%)  profit factor {m['profit_factor']}  max drawdown {m['max_drawdown_pct']}%",
             f"Avg win {m['avg_win']}  avg loss {m['avg_loss']}  expectancy/trade {m['expectancy_per_trade']}",
             f"Max consecutive wins {m['max_consecutive_wins']}  losses {m['max_consecutive_losses']}"]
    for name in ("by_symbol", "by_session", "by_volatility", "by_news", "by_exit_reason"):
        lines.append(f"{name}: {m[name]}")
    if rejected:
        lines.append(f"Rule rejections / execution blocks: {dict(rejected)}")
    return "\n".join(lines)


LIMITATIONS = ("Bar-based test (no ticks): stops assumed to hit before targets inside one bar; constant spread/slippage; "
               "news only if a calendar is supplied; prop guard and correlation filters not simulated. "
               "A backtest is not a forecast; do not tune parameters on the out-of-sample part.")
