"""New replay entry points fail closed and preserve historical gates in search."""
from decimal import Decimal as D

import pytest

from btc_cta.cross_arb.engine import Policy
from btc_cta.cross_arb.entry_filters import EntryFilterConfig
from btc_cta.cross_arb.history_gate import (
    DAY_MS,
    HOUR_MS,
    HistoryGate,
    HourlyBar,
    RollingHistoryConfig,
)
from btc_cta.cross_arb.market import Book, Rules
from btc_cta.cross_arb.replay import ReplayData, ReplayPoint, run_segment
from btc_cta.cross_arb.sweep import build_grid, choose_candidate, run_search


def sample():
    points = [ReplayPoint(t, {
        v: Book(v, ((D(99), D(100)),), ((D(100), D(100)),), t, t, 0)
        for v in ("bybit", "bitget")
    }) for t in (1000, 2000)]
    data = ReplayData(points, {"symbol": "ALTUSDT", "first_ms": 1000, "last_ms": 2000,
                               "archive_sha256": "a" * 64, "verified": True})
    rules = {v: Rules(D(1), D(1), D(5), D(".01")) for v in ("bybit", "bitget")}
    return data, rules


def test_replay_requires_history_before_creating_output(tmp_path):
    data, rules = sample()
    output = tmp_path / "replay"
    with pytest.raises(ValueError, match="historical ranking gate is required"):
        run_segment(data, output, rules, Policy(), EntryFilterConfig(), 1000, 2001)
    assert not output.exists()


def test_search_requires_history_before_reading_or_creating_files(tmp_path):
    output = tmp_path / "search"
    grid = build_grid([60], [D(0)], [D(0)], [D(10)], 1000)
    with pytest.raises(ValueError, match="historical ranking gate is required"):
        run_search(tmp_path, tmp_path / "absent", tmp_path / "absent", output, grid)
    assert not output.exists()


def test_history_ranked_candidate_needs_no_short_term_volume_or_volatility_floor():
    grid = build_grid([60], [D(0)], [D(0)], [D(10)], 1000, history_filtered=True)
    assert [r[1] for r in grid] == ["candidate", "baseline"]
    row = {"candidate_id": "c000", "role": "candidate",
           "config": {"min_volatility_bps": "0", "min_depth_usdt": "0"},
           "train": {"unresolved": False, "closed_pairs": 3, "net_before_funding": "1"},
           "validation": {"unresolved": False, "closed_pairs": 3, "net_before_funding": "2"}}
    assert choose_candidate([row], history_filtered=True)["selected_candidate_id"] == "c000"
    assert choose_candidate([row])["status"] == "NO_QUALIFYING_PARAMETERS"


@pytest.mark.parametrize("verified", [False, True])
def test_actual_replay_applies_history_and_default_depth_sizing(tmp_path, verified):
    import csv
    import json

    end_day = 40 * DAY_MS
    bars = [HourlyBar(t, D(100 + i % 2), D(1)) for i, t in enumerate(
        range(end_day - 20 * DAY_MS - HOUR_MS, end_day, HOUR_MS))]
    gate = HistoryGate(RollingHistoryConfig(), {
        "ALTUSDT": {v: bars for v in ("bybit", "bitget")}
    }, identity_verified_symbols={"ALTUSDT"} if verified else set())
    start = end_day + 61000
    points = []
    for offset in (0, 1000, 2000, 3000):
        now = start + offset
        prices = (("99", "100"), ("102", "103")) if offset < 2000 else (
            ("100", "101"), ("100", "101"))
        books = {v: Book(v, ((D(bid), D(8)),), ((D(ask), D(8)),), now, now, 0)
                 for v, (bid, ask) in zip(("bybit", "bitget"), prices, strict=True)}
        points.append(ReplayPoint(now, books))
    data = ReplayData(points, {"symbol": "ALTUSDT", "first_ms": start,
                               "last_ms": start + 3000, "archive_sha256": "a" * 64,
                               "verified": True})
    _, rules = sample()
    policy = Policy(bybit_fee_bps=D(3), bitget_fee_bps=D(3), max_holding_s=2)
    output = tmp_path / "segment"
    result = run_segment(data, output, rules, policy,
                         EntryFilterConfig(min_volatility_bps=D(0), min_depth_usdt=D(0)),
                         start, start + 3001, history_gate=gate)
    metadata = json.loads((output / "segment.json").read_text())
    assert metadata["legacy_unfiltered"] is False
    assert metadata["history_gate"]["config"]["min_turnover_usdt"] == "0"
    assert metadata["depth_sizing"] == {"band_bps": "10", "participation": "0.25"}
    assert result["completed_cycles"] == int(verified)
    with (output / "orders.csv").open() as f:
        orders = list(csv.DictReader(f))
    assert len(orders) == (4 if verified else 0)
    assert all(D(order["qty"]) == 2 for order in orders)  # 8 visible coins times 25%.
    with (output / "signals.csv").open() as f:
        signals = list(csv.DictReader(f))
    assert signals and all(row["history_rank"] == "1" for row in signals)
    assert all(row["history_filter_reason"] == (
        "history_filter_passed" if verified else "asset_identity_unverified") for row in signals)
