from typing import Dict, List, Optional, Tuple

from backend.app.executor import AccountInfo, BrokerPosition, OrderResult
from backend.app.sizing import SymbolSpec, position_pnl


class FakeBroker:
    """Simulated broker: fills at the quote (+ optional slippage), scripted rejections, broker-side closes."""

    def __init__(self, spec: SymbolSpec, bid=2000.0, ask=2000.2, equity=1000.0, free_margin=1000.0, demo=True,
                 login=555001):
        self._spec = spec
        self.bid, self.ask = bid, ask
        self.acct = AccountInfo(equity, equity, free_margin, demo, "USD", login)
        self.pos: Dict[int, BrokerPosition] = {}
        self.next_ticket = 100
        self.sent: List[tuple] = []
        self.calls: List[str] = []
        self.fail_send_at = set()
        self.slippage = 0.0
        self.fail_modify = False
        self.fail_close = False
        self.history: Dict[int, float] = {}
        self.quote_ok = True
        self.margin_override: Optional[float] = None

    # --- protocol
    def account(self):
        return self.acct

    def spec(self, symbol):
        return self._spec if symbol == self._spec.symbol else None

    def quote(self, symbol) -> Optional[Tuple[float, float]]:
        return (self.bid, self.ask) if self.quote_ok else None

    def margin_required(self, symbol, direction, lots, price):
        if self.margin_override is not None:
            return self.margin_override
        return lots * self._spec.margin_per_lot

    def send_market(self, symbol, direction, lots, sl, tp, comment, deviation_points):
        self.calls.append("send")
        idx = len(self.sent)
        self.sent.append((symbol, direction, lots, sl, tp, comment, deviation_points))
        if idx in self.fail_send_at:
            return OrderResult(False, "REJECT", reason="scripted rejection")
        price = (self.ask + self.slippage) if direction == 1 else (self.bid - self.slippage)
        t = self.next_ticket
        self.next_ticket += 1
        self.pos[t] = BrokerPosition(t, symbol, direction, lots, price, sl, tp, comment)
        return OrderResult(True, "DONE", t, price, lots)

    def modify_sl(self, ticket, sl):
        self.calls.append("modify")
        if self.fail_modify or ticket not in self.pos:
            return OrderResult(False, "MODIFY_FAILED")
        self.pos[ticket].sl = sl
        return OrderResult(True, "DONE", ticket)

    def close_position(self, ticket, lots=None):
        self.calls.append("close")
        if self.fail_close or ticket not in self.pos:
            return OrderResult(False, "CLOSE_FAILED")
        p = self.pos[ticket]
        n = lots if lots is not None else p.lots
        price = self.bid if p.direction == 1 else self.ask
        pnl = position_pnl(self._spec, p.direction, p.entry, price, n)
        if lots is None or abs(n - p.lots) < 1e-9:
            del self.pos[ticket]
        else:
            p.lots = round(p.lots - n, 8)
        return OrderResult(True, "DONE", ticket, price, n, pnl)

    def positions(self):
        return list(self.pos.values())

    def closed_pnl(self, ticket):
        return self.history.get(ticket)

    # --- test helpers
    def broker_closes(self, ticket, pnl):
        """Simulate the broker-side stop/target closing a position."""
        del self.pos[ticket]
        self.history[ticket] = pnl
