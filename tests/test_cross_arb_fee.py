"""Economic regressions for per-fill fees on the SKL paper execution path."""

import hashlib
import json
import sqlite3
from decimal import Decimal as D

import pytest

from btc_cta.cross_arb.engine import Policy
from btc_cta.cross_arb.market import Book, Rules
from btc_cta.cross_arb.paper import PaperSession

VENUES = ("bybit", "bitget")


def _book(venue, bids, asks, now):
    # Each fill consumes two levels: using BBO instead of actual VWAP would fail.
    quantities = (D("12000"), D("36000"))
    return Book(
        venue,
        tuple(zip(map(D, bids), quantities, strict=True)),
        tuple(zip(map(D, asks), quantities, strict=True)),
        now,
        now,
        0,
    )


def _entry(now=10000, wide=False):
    return {
        "bybit": _book("bybit", (".009999", ".009997"), (".010000", ".010002"), now),
        "bitget": _book(
            "bitget",
            (".010050", ".010048") if wide else (".010030", ".010028"),
            (".010051", ".010053") if wide else (".010031", ".010033"),
            now,
        ),
    }


def _exit(now=11000):
    return {
        "bybit": _book("bybit", (".010014", ".010012"), (".010015", ".010017"), now),
        "bitget": _book("bitget", (".010015", ".010013"), (".010016", ".010018"), now),
    }


def _rules():
    return {v: Rules(D("1000"), D("1000"), D("5"), D(".000001")) for v in VENUES}


def _rows(session, table):
    return [
        json.loads(row[0]) for row in session.db.execute(f"SELECT value FROM {table} ORDER BY id")
    ]


def _fee_policy(bps):
    return Policy(bybit_fee_bps=D(bps), bitget_fee_bps=D(bps))


def test_three_bps_roundtrip_vwap_fees_and_independent_account_reconciliation(tmp_path):
    policy = _fee_policy("3")
    assert [policy.fee(v) for v in VENUES] == [D(".0003"), D(".0003")]
    session = PaperSession(tmp_path / "fee3.sqlite", policy, symbol="SKLUSDT")
    try:
        opened = session.tick(_entry(), _rules(), 10000)
        assert opened["status"] == "OPEN"
        assert opened["orders"] == 2
        assert D(opened["cycle"]["qty"]) == D("24000")
        closed = session.tick(_exit(), _rules(), 11000, force_exit_reason="session_end")
        assert closed["cycle"] is None and closed["completed_cycles"] == 1
        orders = _rows(session, "orders")
        cycle = _rows(session, "cycles")[0]
        assert len(orders) == 4
        assert len({row["cycle_id"] for row in orders}) == 1
        assert {row["pair"] for row in orders} == {"SKL/USDT:USDT"}
        assert {D(row["qty"]) for row in orders} == {D("24000")}
        assert [row["side"] for row in orders] == ["buy", "sell", "sell", "buy"]
        # Manually calculated from the two equally consumed 12,000-SKL levels.
        assert [D(row["notional"]) for row in orders] == [
            D("240.024"),
            D("240.696"),
            D("240.312"),
            D("240.408"),
        ]
        assert [D(row["fee"]) for row in orders] == [
            D(".0720072"),
            D(".0722088"),
            D(".0720936"),
            D(".0721224"),
        ]
        assert all(D(row["fee_rate"]) == D(".0003") for row in orders)
        assert D(cycle["gross_pnl"]) == D(".576")
        assert D(cycle["total_fees"]) == D(".2884320")
        assert D(cycle["net_before_funding"]) == D(".2875680")
        assert D(closed["closed_pair_net_before_funding"]) == D(".2875680")
        expected_net = {"bybit": D(".1438992"), "bitget": D(".1436688")}
        for venue in VENUES:
            assert D(cycle[f"{venue}_net_before_funding"]) == expected_net[venue]
            assert D(closed["cash"][venue]) == D("1000") + expected_net[venue]
            assert D(closed["last_equity"][venue]["cash_balance"]) == D(closed["cash"][venue])
            assert D(closed["last_equity"][venue]["reserved_margin"]) == 0
    finally:
        session.close()


def test_same_executable_depth_rejected_at_six_bps_but_filled_at_three(tmp_path):
    observations = {}
    for bps in ("6", "3"):
        session = PaperSession(tmp_path / f"fee{bps}.sqlite", _fee_policy(bps), symbol="SKLUSDT")
        try:
            state = session.tick(_entry(), _rules(), 10000)
            signal = next(row for row in _rows(session, "signals") if row["long_venue"] == "bybit")
            observations[bps] = (state, signal)
        finally:
            session.close()
    old_state, old_signal = observations["6"]
    new_state, new_signal = observations["3"]
    assert old_state["orders"] == 0
    assert old_signal["reason"] == "below_cost_threshold"
    assert not old_signal["eligible"]
    assert new_state["orders"] == 2
    assert new_signal["eligible"] and new_signal["action"] == "ENTERED"
    assert old_signal["gross_edge_bps"] == new_signal["gross_edge_bps"]
    assert old_signal["long_limit"] == new_signal["long_limit"]
    assert old_signal["short_limit"] == new_signal["short_limit"]
    assert D(old_signal["net_edge_bps"]) < D("5") <= D(new_signal["net_edge_bps"])
    assert D(new_signal["net_edge_bps"]) - D(old_signal["net_edge_bps"]) == D("12")


def test_new_fee_policy_cannot_reprice_or_overwrite_historical_ledger(tmp_path):
    old_path = tmp_path / "original-six-bps.sqlite"
    old = PaperSession(old_path, _fee_policy("6"), symbol="SKLUSDT")
    old.tick(_entry(wide=True), _rules(), 10000)
    old.tick(_exit(), _rules(), 11000, force_exit_reason="session_end")
    old.close()
    before = hashlib.sha256(old_path.read_bytes()).hexdigest()
    with pytest.raises(FileExistsError):
        PaperSession(old_path, _fee_policy("3"), symbol="SKLUSDT")
    new = PaperSession(tmp_path / "new-three-bps.sqlite", _fee_policy("3"), symbol="SKLUSDT")
    try:
        new.tick(_entry(wide=True), _rules(), 10000)
        new.tick(_exit(), _rules(), 11000, force_exit_reason="session_end")
        assert all(D(row["fee_rate"]) == D(".0003") for row in _rows(new, "orders"))
    finally:
        new.close()
    assert hashlib.sha256(old_path.read_bytes()).hexdigest() == before
    with sqlite3.connect(f"file:{old_path}?mode=ro", uri=True) as connection:
        historical_orders = [
            json.loads(row[0]) for row in connection.execute("SELECT value FROM orders")
        ]
        historical_state = json.loads(
            connection.execute("SELECT value FROM latest_state").fetchone()[0]
        )
    assert len(historical_orders) == 4
    assert all(D(row["fee_rate"]) == D(".0006") for row in historical_orders)
    assert historical_state["policy"]["bybit_fee_bps"] == "6"
    assert historical_state["policy"]["bitget_fee_bps"] == "6"
