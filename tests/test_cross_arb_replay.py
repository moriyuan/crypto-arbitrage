from __future__ import annotations

import copy
import csv
import gzip
import hashlib
import json
import sqlite3
from dataclasses import replace
from decimal import Decimal as D

import pytest

from btc_cta.cross_arb.engine import Policy
from btc_cta.cross_arb.entry_filters import EntryFilterConfig
from btc_cta.cross_arb.market import Book, Rules
from btc_cta.cross_arb.replay import ReplayData, ReplayPoint, load_archive, run_segment


def raw_frames():
    arg = {"instType": "usdt-futures", "topic": "books", "symbol": "ALTUSDT"}
    return [
        {
            "venue": "bybit",
            "received_at_ms": 1000,
            "payload": {
                "topic": "orderbook.50.ALTUSDT",
                "type": "snapshot",
                "cts": 1000,
                "data": {
                    "s": "ALTUSDT",
                    "seq": 1,
                    "u": 1,
                    "b": [["99", "100"]],
                    "a": [["100", "100"]],
                },
            },
        },
        {
            "venue": "bitget",
            "received_at_ms": 1001,
            "payload": {
                "arg": arg,
                "action": "snapshot",
                "data": [
                    {
                        "seq": 10,
                        "pseq": 0,
                        "ts": "1001",
                        "b": [["102", "100"]],
                        "a": [["103", "100"]],
                    }
                ],
            },
        },
        {
            "venue": "bybit",
            "received_at_ms": 1002,
            "payload": {
                "topic": "publicTrade.ALTUSDT",
                "data": [{"s": "ALTUSDT", "i": "1", "T": 1002, "p": "100", "v": "1", "S": "Buy"}],
            },
        },
        {
            "venue": "bitget",
            "received_at_ms": 1003,
            "payload": {
                "arg": {**arg, "topic": "publicTrade"},
                "action": "snapshot",
                "data": [{"i": "2", "T": "1003", "p": "102", "v": "1", "S": "sell"}],
            },
        },
        {
            "venue": "bybit",
            "received_at_ms": 1100,
            "payload": {
                "topic": "orderbook.50.ALTUSDT",
                "type": "delta",
                "cts": 1100,
                "data": {"s": "ALTUSDT", "seq": 2, "u": 2, "b": [["99", "99"]], "a": []},
            },
        },
        {
            "venue": "bitget",
            "received_at_ms": 1101,
            "payload": {
                "arg": arg,
                "action": "update",
                "data": [{"seq": 11, "pseq": 10, "ts": "1101", "b": [["102", "99"]], "a": []}],
            },
        },
    ]


def archive(tmp_path, frames=None):
    run = tmp_path / "source"
    run.mkdir()
    frames = raw_frames() if frames is None else frames
    meta = {
        "symbol": "ALTUSDT",
        "execution_enabled": False,
        "book_channels": {"bybit": "orderbook.50", "bitget": "books"},
    }
    stream = {v: {"disconnects": 0, "invalidations": 0} for v in ("bybit", "bitget")}
    stream.update(fatal_error=None, dropped_events=0, dropped_updates=0, purged_events=0)
    status = {
        **meta,
        "collection_status": "COMPLETED",
        "status": "COMPLETED",
        "raw_frames": len(frames),
        "stream_before_close": stream,
        "stream": copy.deepcopy(stream),
        "events": {v: {"invalidate": 0} for v in ("bybit", "bitget")},
    }
    (run / "run_metadata.json").write_text(json.dumps(meta))
    (run / "status.json").write_text(json.dumps(status))
    with gzip.open(run / "public-ws.jsonl.gz", "wt") as file:
        for row in frames:
            file.write(json.dumps(row) + "\n")
    return run


def test_load_strictly_reconstructs_receipt_order_and_preserves_raw_book_time(tmp_path):
    frames = raw_frames()
    frames[-1]["received_at_ms"] = 1099
    run = archive(tmp_path, frames)
    data = load_archive(run, "ALTUSDT")
    assert [p.timestamp_ms for p in data.points] == [1001, 1100, 1100]
    assert data.points[-1].books["bitget"].received_at_ms == 1099
    assert data.audit["raw_frames"] == 6 and data.audit["trades"] == 2
    assert data.audit["receipt_regressions"] == 1
    assert (
        data.audit["archive_sha256"]
        == hashlib.sha256((run / "public-ws.jsonl.gz").read_bytes()).hexdigest()
    )
    assert data.audit["verified"] is True


@pytest.mark.parametrize(
    "fault", ["symbol", "channel", "sequence", "gap", "empty", "crc", "json", "snapshot"]
)
def test_archive_faults_are_rejected_without_replay_outputs(tmp_path, fault):
    frames = raw_frames()
    if fault == "symbol":
        frames[4]["payload"]["data"]["s"] = "BTCUSDT"
    elif fault == "channel":
        frames[1]["payload"]["arg"]["topic"] = "books50"
    elif fault == "sequence":
        frames[-1]["payload"]["data"][0]["pseq"] = 100
    elif fault == "gap":
        frames[-1]["received_at_ms"] = 7000
    elif fault == "empty":
        frames = []
    elif fault == "snapshot":
        frames[-1]["payload"]["action"] = "snapshot"
    run = archive(tmp_path, frames)
    path = run / "public-ws.jsonl.gz"
    if fault == "crc":
        body = bytearray(path.read_bytes())
        body[-8] ^= 1
        path.write_bytes(body)
    elif fault == "json":
        with gzip.open(path, "wt") as file:
            file.write("{broken json\n")
    with pytest.raises(ValueError):
        load_archive(run, "ALTUSDT")
    assert sorted(p.name for p in run.iterdir()) == [
        "public-ws.jsonl.gz",
        "run_metadata.json",
        "status.json",
    ]


@pytest.mark.parametrize(
    "field", ["disconnects", "invalidations", "dropped_events", "purged_events"]
)
def test_recorded_stream_discontinuity_blocks_archive(tmp_path, field):
    run = archive(tmp_path)
    status = json.loads((run / "status.json").read_text())
    target = (
        status["stream_before_close"]["bybit"]
        if field in {"disconnects", "invalidations"}
        else status["stream_before_close"]
    )
    target[field] = 1
    (run / "status.json").write_text(json.dumps(status))
    with pytest.raises(ValueError):
        load_archive(run, "ALTUSDT")


def books(now, convergence=False, midpoint=None):
    prices = (("100", "101"), ("100", "101")) if convergence else (("99", "100"), ("102", "103"))
    if midpoint is not None:
        mid = D(midpoint)
        prices = (
            (str(mid - D(".5")), str(mid + D(".5"))),
            (str(mid + D("2.5")), str(mid + D("3.5"))),
        )
    return {
        v: Book(v, ((D(prices[i][0]), D(100)),), ((D(prices[i][1]), D(100)),), now, now, 0)
        for i, v in enumerate(("bybit", "bitget"))
    }


def dataset(points):
    return ReplayData(
        points=points,
        audit={
            "symbol": "ALTUSDT",
            "first_ms": points[0].timestamp_ms,
            "last_ms": points[-1].timestamp_ms,
            "archive_sha256": "a" * 64,
            "verified": True,
        },
    )


def parameters():
    rules = {v: Rules(D(".1"), D(".1"), D(1), D(".001")) for v in ("bybit", "bitget")}
    policy = Policy(
        bybit_fee_bps=D(3),
        bitget_fee_bps=D(3),
        max_holding_s=2,
        cooldown_s=30,
        max_cycles=100,
        take_profit_bps=D(0),
    )
    return rules, policy, EntryFilterConfig(min_volatility_bps=D(0), min_depth_usdt=D(0))


def rows(output, table):
    with sqlite3.connect(output / "ledger.sqlite") as db:
        return [json.loads(row[0]) for row in db.execute(f"SELECT value FROM {table} ORDER BY id")]


def test_segment_keeps_four_equal_quantity_orders_and_reconciles_accounts(tmp_path):
    data = dataset([ReplayPoint(t, books(t, t >= 3000)) for t in (1000, 2000, 3000, 4000)])
    output = tmp_path / "segment"
    progress = []
    summary = run_segment(data, output, *parameters(), 1000, 4001, progress=progress.append,
        allow_legacy_unfiltered=True)
    orders, cycles = rows(output, "orders"), rows(output, "cycles")
    assert len(orders) == 4 and len(cycles) == 1
    assert len({D(row["qty"]) for row in orders}) == 1
    assert all(1000 <= row["timestamp_ms"] < 4001 for row in orders)
    for venue in ("bybit", "bitget"):
        assert D(summary["cash"][venue]) == D(1000) + sum(
            D(c[f"{venue}_net_before_funding"]) for c in cycles
        )
    assert summary["segment_status"] == "COMPLETED"
    assert progress[-1]["processed"] == 4
    assert (output / "segment.json").exists()
    assert all(
        (output / f"{name}.csv").exists() for name in ("signals", "orders", "cycles", "equity")
    )


def test_filter_warmup_has_no_orders_and_does_not_use_current_future_bucket(tmp_path):
    data = dataset(
        [
            ReplayPoint(1000 + i * 1000, books(1000 + i * 1000, midpoint=m))
            for i, m in enumerate(("100", "101", "100", "100", "100", "100"))
        ]
    )
    rules, policy, _ = parameters()
    config = EntryFilterConfig(volatility_window_s=2, min_volatility_bps=D(5), min_depth_usdt=D(0))
    summary = run_segment(data, tmp_path / "segment", rules, policy, config, 4000, 6001,
        allow_legacy_unfiltered=True)
    signals = rows(tmp_path / "segment", "signals")
    assert min(s["timestamp_ms"] for s in signals) == 4000
    assert signals[0]["volatility_samples"] == 2
    assert D(signals[0]["bybit_volatility_bps"]) > 5
    assert summary["warmup_points"] == 3
    assert all(o["timestamp_ms"] >= 4000 for o in rows(tmp_path / "segment", "orders"))


def test_points_after_exclusive_end_do_not_change_fills(tmp_path):
    points = [ReplayPoint(t, books(t, t >= 3000)) for t in (1000, 2000, 3000, 4000, 5000)]
    first = run_segment(dataset(points), tmp_path / "a", *parameters(), 1000, 4000,
        allow_legacy_unfiltered=True)
    changed = points[:-1] + [ReplayPoint(5000, books(5000, midpoint="999"))]
    second = run_segment(dataset(changed), tmp_path / "b", *parameters(), 1000, 4000,
        allow_legacy_unfiltered=True)
    assert first["cash"] == second["cash"]
    columns = ("timestamp_ms", "qty", "price", "venue", "intent")
    assert [tuple(o[k] for k in columns) for o in rows(tmp_path / "a", "orders")] == [
        tuple(o[k] for k in columns) for o in rows(tmp_path / "b", "orders")
    ]


def test_stale_last_in_segment_quote_cannot_be_used_to_force_close(tmp_path):
    data = dataset([ReplayPoint(t, books(t)) for t in (1000, 2000, 6000)])
    rules, policy, config = parameters()
    summary = run_segment(
        data,
        tmp_path / "segment",
        rules,
        replace(policy, stop_loss_usdt=D(100)),
        config,
        1000,
        6000,
     allow_legacy_unfiltered=True)
    assert summary["segment_status"] == "UNRESOLVED"
    assert summary["orders"] == 2 and summary["cycle"] is not None
    assert len(rows(tmp_path / "segment", "orders")) == 2


def test_zero_trades_exports_headers_and_each_direction(tmp_path):
    data = dataset([ReplayPoint(t, books(t, True)) for t in (1000, 2000, 3000)])
    summary = run_segment(data, tmp_path / "segment", *parameters(), 1000, 3001,
        allow_legacy_unfiltered=True)
    assert summary["orders"] == summary["completed_cycles"] == 0
    with (tmp_path / "segment/orders.csv").open() as file:
        assert list(csv.DictReader(file)) == []
    assert len(rows(tmp_path / "segment", "signals")) == 2 * (summary["processed"] + 1)


def test_invalid_bounds_and_existing_output_are_rejected(tmp_path):
    data = dataset([ReplayPoint(t, books(t)) for t in (1000, 2000)])
    with pytest.raises(ValueError):
        run_segment(data, tmp_path / "bad", *parameters(), 1000, 2500, allow_legacy_unfiltered=True)
    assert not (tmp_path / "bad").exists()
    output = tmp_path / "exists"
    output.mkdir()
    (output / "keep").write_text("preserve")
    with pytest.raises(FileExistsError):
        run_segment(data, output, *parameters(), 1000, 2000, allow_legacy_unfiltered=True)
    assert (output / "keep").read_text() == "preserve"


def test_no_new_pair_can_open_at_the_final_holding_window_boundary(tmp_path):
    data = dataset(
        [ReplayPoint(t, books(t, convergence=t < 3000)) for t in (1000, 2000, 3000, 4000, 5000)]
    )
    summary = run_segment(data, tmp_path / "segment", *parameters(), 1000, 5000,
        allow_legacy_unfiltered=True)
    assert summary["orders"] == 0
    eligible = [s for s in rows(tmp_path / "segment", "signals") if s["eligible"]]
    assert eligible and all(s["timestamp_ms"] >= 3000 for s in eligible)
    assert all(s["reason"] == "segment_end" for s in eligible)


def test_future_received_book_rejected_before_creating_ledger(tmp_path):
    data = dataset([ReplayPoint(1000, books(1001)), ReplayPoint(2000, books(2000))])
    with pytest.raises(ValueError):
        run_segment(data, tmp_path / "bad", *parameters(), 1000, 2000, allow_legacy_unfiltered=True)
    assert not (tmp_path / "bad").exists()
