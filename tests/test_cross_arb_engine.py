from dataclasses import replace
from decimal import Decimal as D

import pytest

from btc_cta.cross_arb.engine import Coordinator, Journal, Policy, candidate, executable_pnl
from btc_cta.cross_arb.freqtrade import OrderSnapshot, TradeSnapshot
from btc_cta.cross_arb.market import Book, Rules


def book(venue, bid, ask, now=10000):
    return Book(venue, ((D(bid), D("10")),), ((D(ask), D("10")),), now, now, 10.0)


def rules():
    return {v: Rules(D(".001"), D(".001"), D("5"), D(".1")) for v in ("bybit", "bitget")}


def test_candidate_requires_four_fees_and_matching_quantity():
    p = Policy(max_notional=D("1000"))
    books = {
        "bybit": book("bybit", "99999", "100000"),
        "bitget": book("bitget", "100400", "100401"),
    }
    c = candidate(books, rules(), p, 10000)
    assert c is not None and c["long"] == "bybit"
    assert D(c["qty"]) == D(".009")
    books["bitget"] = book("bitget", "100100", "100101")
    assert candidate(books, rules(), p, 10000) is None


def test_stale_crossed_time_and_missing_depth_rejected():
    books = {
        "bybit": book("bybit", "99999", "100000"),
        "bitget": book("bitget", "100400", "100401"),
    }
    assert candidate(books, rules(), Policy(), 15000) is None
    books["bitget"] = book("bitget", "100400", "100401", now=9000)
    assert candidate(books, rules(), Policy(), 10000) is None


def test_invalid_policy_rejected():
    for kwargs in (
        {"max_notional": D("NaN")},
        {"entry_timeout_s": 0},
        {"bybit_fee_bps": D("-1")},
        {"max_cycles": 0},
    ):
        with pytest.raises(ValueError):
            Policy(**kwargs)


def test_profit_uses_reverse_book_and_four_fees():
    books = {
        "bybit": book("bybit", "100010", "100011"),
        "bitget": book("bitget", "100029", "100030"),
    }
    pnl = executable_pnl(
        D("1"),
        "bybit",
        "bitget",
        D("100000"),
        D("100300"),
        books,
        Policy(bybit_fee_bps=D("5"), bitget_fee_bps=D("5")),
    )
    assert pnl == D("79.83")


class Worker:
    def __init__(self, unknown=False, partial=False, unfilled=False):
        self.current = None
        self.unknown = unknown
        self.partial = partial
        self.unfilled = unfilled
        self.entry_count = self.exit_count = self.cancel_count = 0
        self.defer = None

    def preflight(self, expected_exchange, expect_dry_run=True):
        assert expect_dry_run

    def open_trades(self):
        return [self.current] if self.current and self.current.is_open else []

    def trade(self, trade_id):
        return self.current

    def find_by_tag(self, tag):
        return self.current if self.current and self.current.entry_tag == tag else None

    def force_enter(self, *, pair, side, stakeamount, leverage, price, entry_tag):
        self.entry_count += 1
        assert leverage == 1
        q = stakeamount / price
        fill = D("0") if self.unfilled else q / 2 if self.partial else q
        t = TradeSnapshot(
            1,
            pair,
            entry_tag,
            True,
            side == "short",
            q,
            fill,
            price,
            self.unfilled,
            (
                OrderSnapshot(
                    "entry",
                    "sell" if side == "short" else "buy",
                    self.unfilled,
                    q,
                    fill,
                    "open" if self.unfilled else "closed",
                ),
            ),
        )
        if self.unknown:
            self.defer = t
            raise TimeoutError("lost response")
        self.current = t
        return t

    def force_exit(self, trade_id):
        self.exit_count += 1
        t = self.current
        exit_order = OrderSnapshot(
            "exit",
            "buy" if t.is_short else "sell",
            False,
            t.filled_amount,
            t.filled_amount,
            "closed",
        )
        self.current = replace(
            t,
            is_open=False,
            filled_amount=D(0),
            orders=(*t.orders, exit_order),
            close_profit_abs=D("1"),
        )

    def cancel_open_orders(self, trade_id):
        self.cancel_count += 1
        assert self.current.filled_amount == 0
        self.current = None  # Freqtrade deletes a zero-fill initial trade.
        raise TimeoutError("serialization failed after successful cancellation")


def opportunities():
    return {"bybit": book("bybit", "99999", "100000"), "bitget": book("bitget", "100500", "100501")}


def setup_engine(tmp_path, second=None, policy=None):
    clients = {"bybit": Worker(), "bitget": second or Worker()}
    journal = Journal(tmp_path / "coordinator.sqlite")
    engine = Coordinator(clients, journal, policy or Policy(max_notional=D("1000")))
    return engine, clients, journal


def test_roundtrip_only_clears_after_both_closed(tmp_path):
    e, clients, journal = setup_engine(tmp_path)
    assert e.tick(opportunities(), rules(), 10000) == "ENTERING"
    assert e.tick(opportunities(), rules(), 10001) == "OPEN"
    quotes = {
        "bybit": book("bybit", "100249", "100250", 11000),
        "bitget": book("bitget", "100249", "100250", 11000),
    }
    assert e.tick(quotes, rules(), 11000) == "EXITING"
    assert all(c.exit_count == 1 for c in clients.values())
    assert e.tick(quotes, rules(), 11001) == "FLAT"
    assert e.state["realized_pnl"] == "2"
    assert e.state["completed"] == 1
    journal.close()


def test_lost_entry_response_never_retried_and_late_trade_flattened(tmp_path):
    worker = Worker(unknown=True)
    e, clients, journal = setup_engine(tmp_path, worker)
    assert e.tick(opportunities(), rules(), 10000) == "RECOVERY"
    assert e.tick(None, rules(), 10001) == "RECOVERY"
    assert clients["bybit"].exit_count == 1
    assert e.tick(None, rules(), 11000) == "RECOVERY"
    assert worker.entry_count == 1
    worker.current = worker.defer
    assert e.tick(None, rules(), 12000) == "RECOVERY"
    assert worker.exit_count == 1
    assert e.tick(None, rules(), 12001) == "FLAT"
    journal.close()


def test_partial_fill_aborts_and_exits_both(tmp_path):
    e, clients, journal = setup_engine(tmp_path, Worker(partial=True))
    e.tick(opportunities(), rules(), 10000)
    assert e.tick(None, rules(), 16000) == "RECOVERY"
    assert all(c.exit_count == 1 for c in clients.values())
    assert e.tick(None, rules(), 16001) == "FLAT"
    journal.close()


def test_zero_fill_cancel_502_and_deleted_trade_resolves(tmp_path):
    e, clients, journal = setup_engine(tmp_path, Worker(unfilled=True))
    e.tick(opportunities(), rules(), 10000)
    assert e.tick(None, rules(), 16000) == "RECOVERY"
    assert clients["bitget"].cancel_count == 1
    assert e.tick(None, rules(), 16001) == "FLAT"
    assert clients["bitget"].entry_count == 1
    journal.close()


def test_restart_reconciles_existing_intent_no_duplicate(tmp_path):
    worker = Worker(unknown=True)
    e, clients, journal = setup_engine(tmp_path, worker)
    e.tick(opportunities(), rules(), 10000)
    policy = e.policy
    journal.close()
    reopened = Journal(tmp_path / "coordinator.sqlite")
    restarted = Coordinator(clients, reopened, policy)
    assert restarted.tick(None, rules(), 15000) == "RECOVERY"
    assert all(c.entry_count == 1 for c in clients.values())
    reopened.close()


def test_single_writer_and_policy_binding(tmp_path):
    e, clients, journal = setup_engine(tmp_path)
    e.request_exit()
    with pytest.raises(RuntimeError):
        Journal(tmp_path / "coordinator.sqlite")
    with pytest.raises(ValueError, match="policy"):
        Coordinator(clients, journal, Policy(max_notional=D("500")))
    journal.close()


def test_untagged_unmanaged_trade_blocks_entry(tmp_path):
    e, clients, journal = setup_engine(tmp_path)
    clients["bitget"].current = TradeSnapshot(
        99, "BTC/USDT:USDT", None, True, False, D("1"), D("1"), D("100000"), False
    )
    assert e.tick(opportunities(), rules(), 10000) == "HALTED"
    assert all(c.entry_count == 0 and c.exit_count == 0 for c in clients.values())
    journal.close()


def test_unknown_exit_not_blindly_retried(tmp_path):
    e, clients, journal = setup_engine(tmp_path)
    e.tick(opportunities(), rules(), 10000)
    e.tick(opportunities(), rules(), 10001)
    calls = []

    def lost(trade_id):
        calls.append(trade_id)
        raise TimeoutError()

    clients["bybit"].force_exit = lost
    e.request_exit()
    assert e.tick(None, rules(), 11000) == "EXITING"
    assert e.tick(None, rules(), 12000) == "EXITING"
    assert len(calls) == 1
    assert e.state["cycle"] is not None
    journal.close()


def test_worker_read_failure_still_flattens_other_leg(tmp_path):
    e, clients, journal = setup_engine(tmp_path, Worker(unknown=True))
    e.tick(opportunities(), rules(), 10000)

    def unavailable(*args):
        raise TimeoutError()

    clients["bitget"].open_trades = unavailable
    clients["bitget"].find_by_tag = unavailable
    assert e.tick(None, rules(), 11000) == "RECOVERY"
    assert clients["bybit"].exit_count == 1
    assert e.state["cycle"] is not None
    journal.close()


def test_late_zero_fill_can_be_cancelled_and_resolved(tmp_path):
    w = Worker(unknown=True, unfilled=True)
    e, clients, journal = setup_engine(tmp_path, w)
    e.tick(opportunities(), rules(), 10000)
    w.current = w.defer
    assert e.tick(None, rules(), 11000) == "RECOVERY"
    assert w.cancel_count == 1
    assert e.tick(None, rules(), 12000) == "FLAT"
    journal.close()


def test_stop_persists_until_explicit_resume(tmp_path):
    e, clients, journal = setup_engine(tmp_path)
    e.request_exit()
    new = Coordinator(clients, journal, e.policy)
    assert new.tick(opportunities(), rules(), 10000) == "HALTED"
    assert all(c.entry_count == 0 for c in clients.values())
    new.resume()
    assert new.tick(opportunities(), rules(), 10000) == "ENTERING"
    journal.close()


def test_exchange_clock_skew_rejected_despite_simultaneous_receipt():
    books = opportunities()
    books["bitget"] = replace(books["bitget"], exchange_ts_ms=8500)
    assert candidate(books, rules(), Policy(), 10000) is None


def test_preflight_time_is_charged_to_quote_age(tmp_path):
    import time

    p = Policy(max_notional=D("1000"), max_quote_age_ms=10)
    e, clients, journal = setup_engine(tmp_path, policy=p)

    def delayed(*args, **kwargs):
        time.sleep(0.03)

    clients["bybit"].preflight = delayed
    assert e.tick(opportunities(), rules(), 10000) == "FLAT"
    assert all(c.entry_count == 0 for c in clients.values())
    journal.close()


def test_recovery_rechecks_dry_run_per_worker(tmp_path):
    e, clients, journal = setup_engine(tmp_path)
    e.tick(opportunities(), rules(), 10000)
    e.tick(opportunities(), rules(), 10001)

    def unsafe(*args, **kwargs):
        raise ValueError("dry_run false")

    clients["bitget"].preflight = unsafe
    e.request_exit()
    assert e.tick(None, rules(), 11000) == "EXITING"
    assert clients["bybit"].exit_count == 1
    assert clients["bitget"].exit_count == 0
    assert e.state["cycle"] is not None
    journal.close()
