import csv
import json
from dataclasses import replace
from decimal import Decimal as D

import pytest

from btc_cta.cross_arb.depth_sizing import DepthSizingConfig
from btc_cta.cross_arb.engine import Policy
from btc_cta.cross_arb.market import Book, Rules
from btc_cta.cross_arb.paper import PaperSession


class HistoryStub:
    def __init__(self):
        self.calls = []

    def metadata(self):
        return {"window_days": 20, "top_n": 10}

    def evaluate(self, symbol, now_ms):
        self.calls.append((symbol, now_ms))
        passed = 20000 <= now_ms < 30000
        return {
            "history_filter_passed": passed,
            "history_filter_reason": "eligible" if passed else "outside_top_n",
            "history_rank": 1 if passed else 11,
            "history_window_start_ms": 1,
            "history_window_end_ms": now_ms - 1,
            "bybit_rv20_bps": "100",
            "bitget_rv20_bps": "90",
            "bybit_adv20_usdt": "1",
            "bitget_adv20_usdt": "2",
        }


def quotes(now, depth="8", convergence=False, ask_shift="0"):
    out = {}
    for venue, bid, ask in (
        ("bybit", "10.04" if convergence else "9.99", "10.05" if convergence else "10"),
        ("bitget", "10.04" if convergence else "10.10", "10.05" if convergence else "10.11"),
    ):
        out[venue] = Book(
            venue, ((D(bid), D(depth)),), ((D(ask) + D(ask_shift), D(depth)),), now, now, 0
        )
    return out


def rules():
    return {v: Rules(D(".1"), D(".1"), D("1"), D(".01")) for v in ("bybit", "bitget")}


def rows(p, table):
    return [json.loads(r[0]) for r in p.db.execute(f"SELECT value FROM {table} ORDER BY id")]


def policy():
    return replace(Policy(), bybit_fee_bps=D(3), bitget_fee_bps=D(3), max_holding_s=3600)


def test_history_applies_once_per_tick_only_to_entries_and_csv(tmp_path):
    history = HistoryStub()
    p = PaperSession(tmp_path / "ledger.sqlite", policy(), symbol="SKLUSDT", history_gate=history)
    p.tick(quotes(10000, "100"), rules(), 10000)
    assert not rows(p, "orders")
    assert all(not r["history_filter_passed"] for r in rows(p, "signals"))
    p.reset_entry_filter()
    p.tick(quotes(20000, "100"), rules(), 20000)
    assert p.state["cycle"] is not None
    p.tick(quotes(30000, "100", True), rules(), 30000, "session_end")
    assert p.state["cycle"] is None and len(rows(p, "orders")) == 4
    assert history.calls == [("SKLUSDT", 10000), ("SKLUSDT", 20000), ("SKLUSDT", 30000)]
    p.export(tmp_path / "export")
    with (tmp_path / "export/signals.csv").open() as f:
        signals = list(csv.DictReader(f))
    assert signals[0]["history_rank"] == "11"
    assert signals[2]["history_rank"] == "1"
    assert signals[2]["history_window_end_ms"] == "19999"
    p.close()


def test_depth_scaled_entry_partial_exits_cash_margin_and_side_reuse(tmp_path):
    p = PaperSession(
        tmp_path / "ledger.sqlite",
        policy(),
        symbol="SKLUSDT",
        history_gate=HistoryStub(),
        depth_sizing=DepthSizingConfig(),
    )
    p.tick(quotes(20000), rules(), 20000)
    assert D(p.state["cycle"]["qty"]) == 2
    assert D(p.state["last_equity"]["bybit"]["reserved_margin"]) == 20
    p.tick(quotes(30000, "4", True), rules(), 30000, "session_end")
    assert D(p.state["cycle"]["qty"]) == 1
    assert D(p.state["last_equity"]["bybit"]["reserved_margin"]) == 10
    assert p.state["status"] == "UNRESOLVED"
    before = len(rows(p, "orders"))
    p.tick(quotes(31000, "4", True), rules(), 31000)
    assert len(rows(p, "orders")) == before  # newer timestamps do not replenish unchanged depth
    changed_other_side = quotes(32000, "4", True)
    changed_other_side["bybit"] = replace(changed_other_side["bybit"], asks=((D("10.06"), D(4)),))
    p.tick(changed_other_side, rules(), 32000)
    assert len(rows(p, "orders")) == before
    # Only the displayed increase is replenishment: from 4 to 8 gives 1 coin per leg.
    p.tick(quotes(33000, "8", True), rules(), 33000)
    assert p.state["cycle"] is None
    orders = rows(p, "orders")
    assert len(orders) == 6
    cycle = rows(p, "cycles")[0]
    assert D(cycle["qty"]) == 2 and cycle["exit_parts"] == 2
    for v in ("bybit", "bitget"):
        entries = [o for o in orders if o["venue"] == v and o["intent"] == "entry"]
        exits = [o for o in orders if o["venue"] == v and o["intent"] == "exit"]
        assert sum(D(o["qty"]) for o in entries) == sum(D(o["qty"]) for o in exits) == 2
        assert all(D(o["fee"]) == D(o["notional"]) * D(".0003") for o in entries + exits)
        assert D(p.state["cash"][v]) == D(1000) + D(cycle[f"{v}_net_before_funding"])
        assert D(p.state["last_equity"][v]["reserved_margin"]) == 0
    p.close()


def test_depth_transaction_rollback_does_not_spend_liquidity(tmp_path, monkeypatch):
    p = PaperSession(
        tmp_path / "ledger.sqlite", policy(), symbol="SKLUSDT", depth_sizing=DepthSizingConfig()
    )
    original = p._append

    def fail(table, record):
        if table == "equity":
            raise OSError("test failure")
        original(table, record)

    monkeypatch.setattr(p, "_append", fail)
    with pytest.raises(OSError):
        p.tick(quotes(20000), rules(), 20000)
    assert not rows(p, "orders") and p.state["cycle"] is None
    monkeypatch.setattr(p, "_append", original)
    p.tick(quotes(20000), rules(), 20000)
    assert D(p.state["cycle"]["qty"]) == 2
    p.close()


def test_real_twenty_day_ranking_rolls_entry_and_never_blocks_exit(tmp_path):
    from btc_cta.cross_arb.history_gate import (
        DAY_MS,
        HOUR_MS,
        HistoryGate,
        HourlyBar,
        RollingHistoryConfig,
    )

    start = 100 * DAY_MS
    data = {}
    for symbol in ["SKLUSDT"] + [f"X{i}USDT" for i in range(10)]:
        bars = []
        price = D(100)
        for hour in range(-1, 23 * 24):
            price *= D("1.001") if symbol == "SKLUSDT" else D("1.002")
            multiplier = D(100) if symbol == "SKLUSDT" and hour >= 20 * 24 else D(1)
            if symbol != "SKLUSDT" and hour >= 21 * 24:
                multiplier = D(10000)
            bars.append(HourlyBar(start + hour * HOUR_MS, price * multiplier, D(1)))
        data[symbol] = {v: bars for v in ("bybit", "bitget")}
    gate = HistoryGate(RollingHistoryConfig(), data, identity_verified_symbols={"SKLUSDT"})
    p = PaperSession(tmp_path / "ledger.sqlite", policy(), symbol="SKLUSDT", history_gate=gate)
    first = start + 20 * DAY_MS + 60000
    p.tick(quotes(first, "100"), rules(), first)
    assert p.state["cycle"] is None
    assert rows(p, "signals")[0]["history_rank"] == 11
    p.reset_entry_filter()
    p.tick(None, rules(), first + 1)
    assert gate.evaluate("SKLUSDT", first)["history_rank"] == 11
    second = first + DAY_MS
    p.tick(quotes(second, "100"), rules(), second)
    assert p.state["cycle"] is not None
    assert rows(p, "signals")[-2]["history_rank"] == 1
    third = second + DAY_MS
    p.tick(quotes(third, "100", True), rules(), third)
    assert rows(p, "signals")[-2]["history_rank"] == 11
    assert p.state["cycle"] is None and len(rows(p, "orders")) == 4
    assert p.state["history_gate"]["config"]["window_days"] == 20
    assert not p.state["history_gate"]["historical_turnover_filter_enabled"]
    p.close()


def test_forced_exit_uses_partial_capacity_without_full_book_vwap(tmp_path):
    p = PaperSession(
        tmp_path / "ledger.sqlite", policy(), symbol="SKLUSDT", depth_sizing=DepthSizingConfig()
    )
    p.tick(quotes(20000, "80"), rules(), 20000)
    assert D(p.state["cycle"]["qty"]) == 20
    p.tick(quotes(21000, "4", True), rules(), 21000, "session_end")
    assert D(p.state["cycle"]["qty"]) == 19
    assert p.state["status"] == "UNRESOLVED" and len(rows(p, "orders")) == 4
    p.close()


def test_insufficient_minimum_exit_remains_unresolved(tmp_path):
    p = PaperSession(
        tmp_path / "ledger.sqlite", policy(), symbol="SKLUSDT", depth_sizing=DepthSizingConfig()
    )
    p.tick(quotes(20000), rules(), 20000)
    p.tick(quotes(21000, ".3", True), rules(), 21000, "session_end")
    assert D(p.state["cycle"]["qty"]) == 2
    assert p.state["status"] == "UNRESOLVED" and len(rows(p, "orders")) == 2
    assert not rows(p, "cycles")
    p.close()


def test_depth_sizing_respects_each_account_cash_and_250u(tmp_path):
    p = PaperSession(
        tmp_path / "ledger.sqlite", policy(), symbol="SKLUSDT", depth_sizing=DepthSizingConfig()
    )
    p.state["cash"]["bybit"] = "15"
    p.tick(quotes(20000, "1000"), rules(), 20000)
    assert D(p.state["cycle"]["qty"]) == D("1.4")
    assert D(p.state["last_equity"]["bybit"]["available_balance"]) >= 0
    assert all(D(o["notional"]) <= 250 for o in rows(p, "orders"))
    p.close()


def test_history_error_fails_closed_but_still_exits(tmp_path):
    class SometimesBroken(HistoryStub):
        def evaluate(self, symbol, now_ms):
            if now_ms >= 30000:
                raise ValueError("history unavailable")
            return super().evaluate(symbol, now_ms)

    p = PaperSession(
        tmp_path / "ledger.sqlite", policy(), symbol="SKLUSDT", history_gate=SometimesBroken()
    )
    p.tick(quotes(20000, "100"), rules(), 20000)
    p.tick(quotes(30000, "100", True), rules(), 30000, "session_end")
    assert len(rows(p, "orders")) == 4 and p.state["cycle"] is None
    assert rows(p, "signals")[-1]["history_filter_reason"] == "history_unavailable:ValueError"
    p.close()


def test_depth_fill_vwap_uses_only_participating_unconsumed_price_levels(tmp_path):
    p = PaperSession(
        tmp_path / "ledger.sqlite", policy(), symbol="SKLUSDT", depth_sizing=DepthSizingConfig()
    )
    entry = quotes(20000)
    entry["bybit"] = replace(entry["bybit"], asks=((D(10), D(4)), (D("10.005"), D(4))))
    entry["bitget"] = replace(entry["bitget"], bids=((D("10.10"), D(4)), (D("10.095"), D(4))))
    p.tick(entry, rules(), 20000)
    orders = rows(p, "orders")
    assert D(orders[0]["price"]) == D("10.0025")
    assert D(orders[1]["price"]) == D("10.0975")
    first_exit = quotes(21000, "4", True)
    first_exit["bybit"] = replace(
        first_exit["bybit"], bids=((D("10.04"), D(4)), (D("10.035"), D(4)))
    )
    p.tick(first_exit, rules(), 21000, "session_end")
    assert D(rows(p, "orders")[2]["price"]) == D("10.04")
    second_exit = {
        v: replace(b, exchange_ts_ms=22000, received_at_ms=22000) for v, b in first_exit.items()
    }
    second_exit["bitget"] = replace(second_exit["bitget"], asks=((D("10.05"), D(8)),))
    p.tick(second_exit, rules(), 22000)
    assert D(rows(p, "orders")[4]["price"]) == D("10.035")
    cycle = rows(p, "cycles")[0]
    assert D(cycle["long_exit_price"]) == D("10.0375")
    p.close()


def test_partial_exit_does_not_create_avoidable_minimum_quantity_dust(tmp_path):
    rs = {v: replace(r, min_qty=D(1)) for v, r in rules().items()}
    p = PaperSession(
        tmp_path / "ledger.sqlite", policy(), symbol="SKLUSDT", depth_sizing=DepthSizingConfig()
    )
    p.tick(quotes(20000), rs, 20000)
    p.tick(quotes(21000, "6", True), rs, 21000, "session_end")
    assert D(p.state["cycle"]["qty"]) == 1
    p.tick(quotes(22000, "8", True), rs, 22000)
    assert p.state["cycle"] is None
    assert len(rows(p, "orders")) == 6
    p.close()


def test_take_profit_uses_available_fragment_when_full_depth_is_unavailable(tmp_path):
    p = PaperSession(
        tmp_path / "ledger.sqlite", policy(), symbol="SKLUSDT", depth_sizing=DepthSizingConfig()
    )
    p.tick(quotes(20000, "80"), rules(), 20000)
    p.tick(quotes(21000, "4", True), rules(), 21000)
    assert D(p.state["cycle"]["qty"]) == 19
    assert p.state["cycle"]["pending_exit_reason"] == "take_profit"
    evidence = p.state["cycle"]["exit_evaluation"]
    assert evidence["mode"] == "partial_depth_position_pnl_proxy"
    assert D(evidence["executable_qty"]) == 1
    assert D(evidence["position_qty"]) == 20
    assert p.state["last_equity"]["bybit"]["mark_status"] == "insufficient_depth"
    p.close()
