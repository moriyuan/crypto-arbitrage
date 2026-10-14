import csv
import json
import sqlite3
from dataclasses import replace
from decimal import Decimal as D

import pytest

from btc_cta.cross_arb.engine import Policy
from btc_cta.cross_arb.market import Book, Rules
from btc_cta.cross_arb.paper import PaperSession


def book(venue, bid, ask, now=10000, qty="10"):
    return Book(venue, ((D(bid), D(qty)),), ((D(ask), D(qty)),), now, now, 0)


def books(now=10000, convergence=False):
    return {
        "bybit": book(
            "bybit",
            "100249" if convergence else "99999",
            "100250" if convergence else "100000",
            now,
        ),
        "bitget": book(
            "bitget",
            "100249" if convergence else "100500",
            "100250" if convergence else "100501",
            now,
        ),
    }


def rules():
    return {v: Rules(D(".001"), D(".001"), D("5"), D(".1")) for v in ("bybit", "bitget")}


def read(session, table):
    return [
        json.loads(row[0]) for row in session.db.execute(f"SELECT value FROM {table} ORDER BY id")
    ]


def test_roundtrip_pnl_cash_orders_and_margin(tmp_path):
    p = PaperSession(tmp_path / "paper.sqlite", Policy())
    result = p.tick(books(), rules(), 10000)
    assert result["status"] == "OPEN"
    assert result["orders"] == 2
    assert D(result["cycle"]["qty"]) == D(".002")
    assert D(result["last_equity"]["bybit"]["reserved_margin"]) == D("200")
    assert D(result["cash"]["bybit"]) == D("999.88")
    assert D(result["last_equity"]["bybit"]["available_balance"]) == D("799.88")
    result = p.tick(books(11000, True), rules(), 11000)
    assert result["status"] == "FLAT"
    assert result["completed_cycles"] == 1 and result["orders"] == 4
    cycle = read(p, "cycles")[0]
    assert D(cycle["gross_pnl"]) == D(".998")
    assert D(cycle["total_fees"]) == D(".4811988")
    assert D(cycle["net_before_funding"]) == D(".5168012")
    assert sum(D(x) for x in result["cash"].values()) == D("2000.5168012")
    assert cycle["funding"] == "not_modelled"
    assert len(read(p, "signals")) == 4
    assert len(read(p, "equity")) == 4
    assert all(row["mode"] == "ideal_signal_fill" for row in read(p, "orders"))
    assert [row["side"] for row in read(p, "orders")] == ["buy", "sell", "sell", "buy"]
    p.close()


def test_below_threshold_records_both_directions_without_orders(tmp_path):
    p = PaperSession(tmp_path / "paper.sqlite", Policy())
    p.tick(books(convergence=True), rules(), 10000)
    signals = read(p, "signals")
    assert len(signals) == 2
    assert all(s["reason"] == "below_cost_threshold" for s in signals)
    assert read(p, "orders") == []
    p.close()


def test_invalid_feed_distinguished_and_never_filled(tmp_path):
    p = PaperSession(tmp_path / "paper.sqlite", Policy())
    p.tick(None, rules(), 10000)
    p.tick(books(), rules(), 20000)
    assert [s["reason"] for s in read(p, "signals")] == [
        "missing_feed",
        "missing_feed",
        "stale_or_unsynchronized_feed",
        "stale_or_unsynchronized_feed",
    ]
    assert read(p, "orders") == []
    p.close()


def test_stale_force_exit_stays_unresolved_until_fresh(tmp_path):
    p = PaperSession(tmp_path / "paper.sqlite", Policy())
    p.tick(books(), rules(), 10000)
    result = p.tick(None, rules(), 11000, force_exit_reason="session_end")
    assert result["status"] == "UNRESOLVED"
    assert result["orders"] == 2
    assert result["last_equity"]["bybit"]["equity"] is None
    result = p.tick(books(12000, True), rules(), 12000)
    assert result["status"] == "FLAT" and result["stop_requested"]
    assert read(p, "cycles")[0]["exit_reason"] == "session_end"
    p.tick(books(50000), rules(), 50000)
    assert len(read(p, "orders")) == 4
    p.close()


def test_balance_constraint_includes_entry_fee(tmp_path):
    p = PaperSession(tmp_path / "paper.sqlite", Policy(max_notional=D("1001")))
    quote = {"bybit": book("bybit", "99500", "99501"), "bitget": book("bitget", "100000", "100001")}
    p.tick(quote, rules(), 10000)
    assert read(p, "orders") == []
    assert any(s["reason"] == "insufficient_collateral" for s in read(p, "signals"))
    p.close()


def test_independent_account_balance_cannot_use_other_account_collateral(tmp_path):
    p = PaperSession(tmp_path / "paper.sqlite", Policy())
    p.state["cash"]["bybit"] = "199.99"
    p.state["cash"]["bitget"] = "2000"
    p.tick(books(), rules(), 10000)
    assert read(p, "orders") == []
    assert any(s["reason"] == "insufficient_collateral" for s in read(p, "signals"))
    p.close()


def test_timeout_and_no_exit_with_insufficient_depth(tmp_path):
    p = PaperSession(tmp_path / "paper.sqlite", replace(Policy(), max_holding_s=1))
    p.tick(books(), rules(), 10000)
    thin = {v: book(v, "100249", "100250", 12000, ".0001") for v in ("bybit", "bitget")}
    result = p.tick(thin, rules(), 12000)
    assert result["status"] == "UNRESOLVED" and result["orders"] == 2
    result = p.tick(books(13000), rules(), 13000)
    assert result["status"] == "FLAT"
    assert read(p, "cycles")[0]["exit_reason"] == "holding_timeout"
    p.close()


def test_loss_and_cycle_caps_and_cooldown(tmp_path):
    policy = replace(Policy(), max_cycles=1, max_total_loss_usdt=D(".01"), max_holding_s=1)
    p = PaperSession(tmp_path / "paper.sqlite", policy)
    p.tick(books(), rules(), 10000)
    p.tick(books(11000), rules(), 11000)
    result = p.tick(books(50000), rules(), 50000)
    assert result["halt_reason"] == "total_loss_cap"
    assert result["orders"] == 4
    p.close()


def test_common_quantity_and_vwap_use_multiple_levels(tmp_path):
    p = PaperSession(tmp_path / "paper.sqlite", Policy())
    quote = books()
    quote["bybit"] = Book(
        "bybit",
        ((D("99999"), D("10")),),
        ((D("100000"), D(".001")), (D("100010"), D("10"))),
        10000,
        10000,
        0,
    )
    rs = rules()
    rs["bitget"] = replace(rs["bitget"], qty_step=D(".0003"))
    p.tick(quote, rs, 10000)
    assert read(p, "orders") == []  # common step .003 exceeds allowed .002 BTC
    rs["bitget"] = replace(rs["bitget"], qty_step=D(".0001"))
    p.tick(quote, rs, 10001)
    order = read(p, "orders")[0]
    assert D(order["price"]) == D("100005")
    assert D(order["qty"]) == D(".002")
    p.close()


def test_existing_file_and_nonfinite_balance_refused(tmp_path):
    path = tmp_path / "paper.sqlite"
    p = PaperSession(path, Policy())
    with pytest.raises(FileExistsError):
        PaperSession(path, Policy())
    with pytest.raises(ValueError):
        PaperSession(tmp_path / "bad.sqlite", Policy(), D("NaN"))
    assert path.stat().st_mode & 0o777 == 0o600
    p.close()


def test_append_only_records_enforced(tmp_path):
    p = PaperSession(tmp_path / "paper.sqlite", Policy())
    p.tick(books(), rules(), 10000)
    with pytest.raises(sqlite3.IntegrityError):
        p.db.execute("DELETE FROM orders")
    with pytest.raises(sqlite3.IntegrityError):
        p.db.execute("UPDATE orders SET value='{}'")
    p.close()


def test_tick_transaction_rolls_back_state_and_records(tmp_path, monkeypatch):
    p = PaperSession(tmp_path / "paper.sqlite", Policy())
    original = p._append

    def fail(table, record):
        if table == "equity":
            raise OSError("disk failure")
        original(table, record)

    monkeypatch.setattr(p, "_append", fail)
    with pytest.raises(OSError):
        p.tick(books(), rules(), 10000)
    assert p.state["cycle"] is None
    assert p.state["ticks"] == 0
    assert read(p, "orders") == [] and read(p, "signals") == []
    p.close()


def test_export_has_headers_even_when_no_fills(tmp_path):
    p = PaperSession(tmp_path / "paper.sqlite", Policy())
    p.tick(books(convergence=True), rules(), 10000)
    paths = p.export(tmp_path / "exports")
    assert set(paths) == {"signals", "orders", "cycles", "equity", "summary"}
    with open(paths["orders"]) as stream:
        assert list(csv.DictReader(stream)) == []
    summary = json.loads((tmp_path / "exports" / "summary.json").read_text())
    assert summary["funding_model"] == "not_modelled"
    assert summary["closed_pair_net_before_funding"] == "0"
    p.close()


def test_monotonic_clock_and_closed_session(tmp_path):
    p = PaperSession(tmp_path / "paper.sqlite", Policy())
    p.tick(None, rules(), 10000)
    with pytest.raises(ValueError):
        p.tick(None, rules(), 9999)
    p.close()
    with pytest.raises(RuntimeError):
        p.tick(None, rules(), 11000)


def test_funding_guard_blocks_only_until_next_safe_tick(tmp_path):
    p = PaperSession(tmp_path / "paper.sqlite", Policy())
    result = p.tick(books(), rules(), 10000, force_exit_reason="funding_guard:schedule_unknown")
    assert result["status"] == "FLAT" and not result["stop_requested"]
    assert read(p, "orders") == []
    result = p.tick(books(11000), rules(), 11000)
    assert result["status"] == "OPEN"
    result = p.tick(books(12000), rules(), 12000, force_exit_reason="funding_guard:near_settlement")
    assert result["status"] == "FLAT" and not result["stop_requested"]
    assert read(p, "cycles")[0]["exit_reason"] == "funding_guard:near_settlement"
    result = p.tick(books(50000), rules(), 50000)
    assert result["status"] == "OPEN"
    p.close()


def test_cycle_cap_and_cooldown_skip_eligible_signals(tmp_path):
    p = PaperSession(tmp_path / "paper.sqlite", replace(Policy(), max_cycles=1))
    p.tick(books(), rules(), 10000)
    p.tick(books(11000, True), rules(), 11000)
    p.tick(books(50000), rules(), 50000)
    assert any(s["reason"] == "max_cycles" for s in read(p, "signals"))
    assert len(read(p, "orders")) == 4
    p.close()
    p = PaperSession(tmp_path / "cooldown.sqlite", Policy())
    p.tick(books(), rules(), 10000)
    p.tick(books(11000, True), rules(), 11000)
    p.tick(books(12000), rules(), 12000)
    assert any(s["reason"] == "cooldown" for s in read(p, "signals"))
    assert len(read(p, "orders")) == 4
    p.close()


def test_stop_loss_exits_at_reverse_depth_after_four_fees(tmp_path):
    p = PaperSession(tmp_path / "paper.sqlite", replace(Policy(), stop_loss_usdt=D(".1")))
    p.tick(books(), rules(), 10000)
    p.tick(books(11000), rules(), 11000)
    assert read(p, "cycles")[0]["exit_reason"] == "stop_loss"
    assert D(read(p, "cycles")[0]["net_before_funding"]) < D("-.1")
    p.close()


def test_extreme_guard_rejects_without_division_error(tmp_path):
    p = PaperSession(tmp_path / "paper.sqlite", replace(Policy(), price_guard_bps=D("20000")))
    p.tick(books(), rules(), 10000)
    assert read(p, "orders") == []
    p.close()


def test_skl_roundtrip_equal_base_quantity_actual_vwap_and_symbol_exports(tmp_path):
    p = PaperSession(tmp_path / "skl.sqlite", Policy(), symbol="SKLUSDT")
    rs = {
        "bybit": Rules(D("1"), D("1"), D("5"), D(".00001")),
        "bitget": Rules(D("10"), D("10"), D("5"), D(".00001")),
    }
    quote = {
        "bybit": Book(
            "bybit",
            ((D(".03999"), D("20000")),),
            ((D(".04000"), D("3100")), (D(".04001"), D("16900"))),
            10000, 10000, 0,
        ),
        "bitget": book("bitget", ".04030", ".04031", qty="20000"),
    }
    opened = p.tick(quote, rs, 10000)
    assert opened["status"] == "OPEN"
    assert opened["symbol"] == "SKLUSDT" and opened["pair"] == "SKL/USDT:USDT"
    assert D(opened["cycle"]["qty"]) == D("6200")
    entry_orders = read(p, "orders")
    assert D(entry_orders[0]["price"]) == D(".040005")
    assert D(entry_orders[1]["price"]) == D(".04030")
    result = p.tick(
        {v: book(v, ".04014", ".04015", 11000, "20000") for v in rs}, rs, 11000
    )
    assert result["status"] == "FLAT" and result["completed_cycles"] == 1
    orders = read(p, "orders")
    assert len(orders) == 4
    assert {D(row["qty"]) for row in orders} == {D("6200")}
    assert [row["side"] for row in orders] == ["buy", "sell", "sell", "buy"]
    cycle = read(p, "cycles")[0]
    for venue in rs:
        entry, exit_order = [row for row in orders if row["venue"] == venue]
        direction = D(1) if entry["position_side"] == "long" else D(-1)
        pnl = D("6200") * (D(exit_order["price"]) - D(entry["price"])) * direction
        pnl -= D(entry["fee"]) + D(exit_order["fee"])
        assert D(cycle[f"{venue}_net_before_funding"]) == pnl
        assert D(result["cash"][venue]) == D("1000") + pnl
    assert sum(D(v) - D("1000") for v in result["cash"].values()) == D(cycle["net_before_funding"])
    for table in ("signals", "orders", "cycles", "equity", "events"):
        assert all(
            row["symbol"] == "SKLUSDT" and row["pair"] == "SKL/USDT:USDT"
            for row in read(p, table)
        )
    paths = p.export(tmp_path / "exports")
    for name in ("signals", "orders", "cycles", "equity"):
        with open(paths[name]) as stream:
            rows = list(csv.DictReader(stream))
        assert rows and all(
            row["symbol"] == "SKLUSDT" and row["pair"] == "SKL/USDT:USDT" for row in rows
        )
    assert json.loads((tmp_path / "exports" / "summary.json").read_text())["symbol"] == "SKLUSDT"
    p.close()


@pytest.mark.parametrize("symbol", ["sklusdt", "SKL/USDT:USDT", "SKLUSDC", "BTCUSDT;", None])
def test_invalid_symbol_refused_before_ledger_creation(tmp_path, symbol):
    path = tmp_path / "invalid.sqlite"
    with pytest.raises(ValueError):
        PaperSession(path, Policy(), symbol=symbol)
    assert not path.exists()


def test_default_session_retains_btc_identity(tmp_path):
    p = PaperSession(tmp_path / "btc.sqlite", Policy())
    result = p.tick(books(), rules(), 10000)
    assert result["symbol"] == "BTCUSDT"
    assert result["pair"] == "BTC/USDT:USDT"
    assert all(row["pair"] == "BTC/USDT:USDT" for row in read(p, "orders"))
    p.close()
