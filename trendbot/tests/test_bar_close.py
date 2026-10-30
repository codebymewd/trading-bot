"""Candle-close discipline tests (the candle-close bug fix, requirement #7).

Covered: new M5 candle, multiple ticks on the same candle, same signal evaluated once,
candle closes, EA restart / backend reconnect (persisted gate), duplicate prevention."""
from backend.app.bar_close import (BarGate, BarIdentity, BarSnapshot, TF_SECONDS,
                                   closed_history, is_closed, make_identity)


def bar(open_time, next_open=None, close=1.0):
    return BarSnapshot(open_time=open_time, open=close, high=close, low=close, close=close,
                       next_open_time=next_open)


M5 = 300


def test_forming_candle_is_never_closed():
    # shift-0 bar with no proven next bar => FORMING => never usable as confirmed information
    assert is_closed(bar(1_700_000_000)) is False


def test_new_m5_candle_proves_previous_bar_closed():
    forming = bar(1_700_000_300)                    # newest bar (just opened)
    previous = bar(1_700_000_000, next_open=forming.open_time)
    assert is_closed(previous) is True              # the JUST-CLOSED candle is usable
    assert is_closed(forming) is False              # the new bar itself is still forming


def test_server_time_fallback_is_conservative():
    b = bar(1_700_000_000)
    assert is_closed(b, current_server_time=1_700_000_299, tf_seconds=M5) is False
    assert is_closed(b, current_server_time=1_700_000_300, tf_seconds=M5) is True


def test_closed_history_drops_the_forming_bar():
    bars = [bar(t, next_open=t + M5) for t in (0, M5, 2 * M5)]
    bars.append(bar(3 * M5))                        # newest = forming
    out = closed_history("XAUUSDm", "M5", bars)
    assert [b.open_time for b in out] == [0, M5, 2 * M5]


class FakeDb:
    """Minimal kv store standing in for the SQLite db (persists across 'restarts')."""
    def __init__(self):
        self.kv = {}

    def kv_get(self, key, default=None):
        return self.kv.get(key, default)

    def kv_set(self, key, value):
        self.kv[key] = value


def ident(open_time, symbol="XAUUSDm", tf="M5"):
    return BarIdentity(symbol, tf, open_time)


def test_multiple_ticks_same_candle_evaluate_once():
    db = FakeDb()
    gate = BarGate(db)
    runs = []
    fn = lambda: runs.append(1)
    b = ident(1_700_000_000)
    assert gate.evaluate_once(b, fn) is True         # first tick after the close
    for _ in range(50):                              # many more ticks on the SAME candle
        assert gate.evaluate_once(b, fn) is False
    assert len(runs) == 1                            # evaluated exactly once


def test_next_candle_runs_again():
    db = FakeDb()
    gate = BarGate(db)
    runs = []
    assert gate.evaluate_once(ident(1_700_000_000), lambda: runs.append(1)) is True
    assert gate.evaluate_once(ident(1_700_000_300), lambda: runs.append(1)) is True   # NEW closed bar
    assert len(runs) == 2


def test_backend_restart_does_not_reprocess_consumed_bar():
    db = FakeDb()                                     # db survives the 'restart'
    gate1 = BarGate(db)
    ran = []
    gate1.evaluate_once(ident(1_700_000_000), lambda: ran.append(1))
    gate2 = BarGate(db)                               # fresh process, empty memory, same db
    again = []
    assert gate2.evaluate_once(ident(1_700_000_000), lambda: again.append(1)) is False
    assert again == []                                # duplicate blocked after restart
    newer = []
    assert gate2.evaluate_once(ident(1_700_000_300), lambda: newer.append(1)) is True


def test_ea_restart_backend_reconnect_same_bar_id_stable():
    # bar identity is symbol+timeframe+open_time only - stable across ticks/restarts/reconnects
    a = make_identity("XAUUSDm", "M5", bar(1_700_000_000, next_open=1_700_000_300))
    b = make_identity("XAUUSDm", "M5", bar(1_700_000_000))
    assert a.key() == b.key() == "XAUUSDm|M5|1700000000"
    db = FakeDb()
    gate = BarGate(db)
    runs = []
    gate.evaluate_once(a, lambda: runs.append(1))
    # EA restarted and re-reported the same closed bar on reconnect: must not re-fire
    assert gate.evaluate_once(b, lambda: runs.append(1)) is False
    assert len(runs) == 1


def test_older_bars_never_reprocessed_after_backfill():
    db = FakeDb()
    gate = BarGate(db)
    gate.evaluate_once(ident(1_700_000_600), lambda: None)
    assert gate.already_processed(ident(1_700_000_300)) is True    # older than last processed
    assert gate.evaluate_once(ident(1_700_000_300),
                              lambda: (_ for _ in ()).throw(AssertionError())) is False


def test_mark_processed_blocks_rejected_signal_retry():
    db = FakeDb()
    gate = BarGate(db)
    i = ident(1_700_000_000)
    gate.mark_processed(i)                            # e.g. risk-gate rejected this bar
    assert gate.evaluate_once(i, lambda: (_ for _ in ()).throw(AssertionError())) is False


def test_isolation_symbol_and_timeframe():
    db = FakeDb()
    gate = BarGate(db)
    runs = []
    assert gate.evaluate_once(ident(1_700_000_000), lambda: runs.append(1)) is True
    assert gate.evaluate_once(ident(1_700_000_000, symbol="EURUSD"), lambda: runs.append(1)) is True
    assert gate.evaluate_once(ident(1_700_000_000, tf="M15"), lambda: runs.append(1)) is True
    assert len(runs) == 3                             # different symbols/timeframes are separate bars
