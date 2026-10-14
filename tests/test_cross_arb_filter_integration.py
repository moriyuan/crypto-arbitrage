"""Entry filters gate openings, preserve exits, and leave an auditable record."""
import csv
import json
from decimal import Decimal as D

import pytest

from btc_cta.cross_arb.engine import Policy
from btc_cta.cross_arb.entry_filters import EntryFilterConfig
from btc_cta.cross_arb.market import Book, Rules
from btc_cta.cross_arb.paper import PaperSession


def books(now, factor="1", qty="10"):
    f = D(factor)
    return {
        v: Book(v, ((D(b) * f, D(qty)),), ((D(a) * f, D(qty)),), now, now, 0)
        for v, b, a in (("bybit", "99999", "100000"), ("bitget", "100500", "100501"))
    }


def rules():
    return {v: Rules(D(".001"), D(".001"), D(5), D(".1")) for v in ("bybit", "bitget")}


def rows(p, table="signals"):
    return [json.loads(r[0]) for r in p.db.execute(f"SELECT value FROM {table} ORDER BY id")]


def session(path):
    return PaperSession(path, Policy(), entry_filter=EntryFilterConfig(volatility_window_s=2))


def warm(p):
    for now, factor in ((10000, "1"), (11000, "1.001"), (12000, "1")):
        assert p.tick(books(now, factor), rules(), now)["orders"] == 0


def test_both_thresholds_required_and_saved_to_signal_csv(tmp_path):
    p = session(tmp_path / "ledger.sqlite")
    try:
        warm(p)
        assert p.tick(books(13000, qty=".003"), rules(), 13000)["orders"] == 0
        rejected = rows(p)[-2]
        assert rejected["net_edge_bps"] and not rejected["eligible"]
        assert rejected["reason"] == "liquidity_below_threshold"
        assert D(rejected["bybit_volatility_bps"]) >= 5
        assert D(rejected["long_depth_usdt"]) == 300
        assert p.tick(books(13100), rules(), 13100)["orders"] == 2
        accepted = rows(p)[-2]
        assert accepted["eligible"] and accepted["entry_filter_passed"]
        assert accepted["action"] == "ENTERED"
        assert accepted["volatility_samples"] == 2
        assert p.summary()["entry_filter"]["min_depth_usdt"] == "500"
        p.export(tmp_path)
        with (tmp_path / "signals.csv").open() as f:
            exported = list(csv.DictReader(f))
        assert exported[-2]["bybit_volatility_bps"] == accepted["bybit_volatility_bps"]
        assert exported[-4]["entry_filter_reason"] == "liquidity_below_threshold"
    finally:
        p.close()


def test_current_unfinished_bucket_does_not_turn_flat_window_volatile(tmp_path):
    p = session(tmp_path / "ledger.sqlite")
    try:
        for now in (10000, 11000, 12000):
            p.tick(books(now), rules(), now)
        state = p.tick(books(13000, "1.1"), rules(), 13000)
        assert state["orders"] == 0
        assert rows(p)[-2]["reason"] == "volatility_below_threshold"
    finally:
        p.close()


def test_filter_warmup_and_thin_depth_never_block_forced_exit(tmp_path):
    p = session(tmp_path / "ledger.sqlite")
    try:
        warm(p)
        assert p.tick(books(13000), rules(), 13000)["orders"] == 2
        p.reset_entry_filter()
        state = p.tick(books(14000, qty=".003"), rules(), 14000, force_exit_reason="session_end")
        assert state["cycle"] is None and state["orders"] == 4
        assert len(rows(p, "cycles")) == 1
        assert not rows(p)[-2]["entry_filter_passed"]
    finally:
        p.close()


def test_long_gap_requires_warmup_without_jump_volatility(tmp_path):
    p = session(tmp_path / "ledger.sqlite")
    try:
        warm(p)
        state = p.tick(books(3600000, "1.1"), rules(), 3600000)
        assert state["orders"] == 0
        assert rows(p)[-2]["reason"] == "entry_filter_warmup"
    finally:
        p.close()


def test_failed_tick_rolls_back_filter_samples_with_ledger(tmp_path, monkeypatch):
    p = session(tmp_path / "ledger.sqlite")
    try:
        p.tick(books(10000), rules(), 10000)
        original = p._mark
        monkeypatch.setattr(p, "_mark", lambda *a: (_ for _ in ()).throw(RuntimeError("disk")))
        with pytest.raises(RuntimeError, match="disk"):
            p.tick(books(11000, "1.1"), rules(), 11000)
        monkeypatch.setattr(p, "_mark", original)
        for now in (11000, 12000, 13000):
            state = p.tick(books(now), rules(), now)
        assert state["orders"] == 0
        assert rows(p)[-2]["reason"] == "volatility_below_threshold"
    finally:
        p.close()
