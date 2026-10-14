from __future__ import annotations

import numpy as np
import pytest

from btc_cta.cross_arb.forecast import LinearNowcast, label_index, segment_indices


def test_label_uses_later_received_exchange_state_with_bounded_offset():
    source = np.array([100, 120, 140])
    received = np.array([180, 190, 200])
    assert label_index(source, received, 111) == 0
    assert label_index(source, received, 121) == 1
    assert label_index(source, received, 80) is None
    assert label_index(source, received, 50) is None
    assert label_index(source, received, 141) is None
    assert label_index(source, np.array([90, 110, 130]), 111) is None


def test_chronological_segments_purge_labels_revealed_after_boundary():
    decisions = np.array([100, 300, 5800, 6100, 7700, 8100, 9300])
    revealed = np.array([110, 310, 6100, 6120, 7720, 8120, 9320])
    train, calibration, test = segment_indices(decisions, revealed, 0, 10000, embargo_ms=100)
    assert train.tolist() == [0, 1]
    assert calibration.tolist() == [3, 4]
    assert test.tolist() == [5, 6]


def test_fit_parameters_depend_only_on_supplied_training_rows():
    x = np.array([[float(i), float(i % 2)] for i in range(100)])
    y = 0.02 * x[:, 0]
    model = LinearNowcast.fit(x[:60], y[:60], ["x", "parity"])
    expected = model.predict(x[:60])
    x[60:] = 1e9
    y[60:] = -1e9
    again = LinearNowcast.fit(x[:60], y[:60], ["x", "parity"])
    np.testing.assert_allclose(again.predict(x[:60]), expected)
    assert np.mean(abs(expected - y[:60])) < 0.1


def test_nonfinite_or_incompatible_model_inputs_fail_closed():
    with pytest.raises(ValueError):
        LinearNowcast.fit(np.array([[np.nan]]), np.array([0.0]), ["x"])
    model = LinearNowcast.fit(np.arange(20.0).reshape(-1, 1), np.arange(20.0) * 0.01, ["x"])
    with pytest.raises(ValueError):
        model.predict(np.ones((1, 2)))
    with pytest.raises(ValueError):
        LinearNowcast.from_json(
            {"names": ["x"], "mean": [0], "scale": [0], "coef": [1], "intercept": 0}
        )


def test_model_roundtrip_preserves_predictions():
    x = np.arange(30.0).reshape(-1, 1)
    m = LinearNowcast.fit(x, x[:, 0] * 0.01, ["x"])
    np.testing.assert_allclose(LinearNowcast.from_json(m.to_json()).predict(x), m.predict(x))


def test_future_source_book_cannot_pass_per_venue_gate():
    from types import SimpleNamespace

    from btc_cta.cross_arb.forecast import estimate

    m = LinearNowcast.fit(np.arange(20.0).reshape(-1, 1), np.zeros(20), ["x"])
    features = {
        "numeric": {"x": 5.0, "book_age_ms": 0.0, "receive_age_ms": 0.0},
        "midpoint": 100.0,
        "bestbid": 99.0,
        "bestask": 101.0,
        "book_ts_ms": 200,
        "book_received_ms": 100,
        "projected_book": SimpleNamespace(bids=[], asks=[]),
    }
    result = estimate(
        features, {"model": m.to_json(), "q95_bps": 1.0, "calibration_approved": True}
    )
    assert result["gate"] is False
    assert result["source_age_ms"] == -100


def test_live_no_data_is_not_reported_success(monkeypatch, tmp_path):
    import json

    import btc_cta.cross_arb.forecast as module
    import btc_cta.cross_arb.stream as stream_module

    class EmptyFeed:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def next_event(self, **kwargs):
            return None

        def stats(self):
            return {"fatal_error": None}

    clock = iter([i * 0.25 for i in range(30)])
    monkeypatch.setattr(module.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(stream_module, "StreamBooks", EmptyFeed)
    path = tmp_path / "model.json"
    path.write_text(
        json.dumps({"version": 2, "symbol": "SKLUSDT",
                    "book_channels": {"bybit": "orderbook.50", "bitget": "books"},
                    "venues": {"bybit": {}, "bitget": {}}, "sample_ms": 50})
    )
    module.live(path, tmp_path / "run", 0.5, symbol="SKLUSDT")
    status = json.loads((tmp_path / "run/status.json").read_text())
    assert status["status"] == "NO_PAIRED_DATA"
    assert status["execution_enabled"] is False


@pytest.mark.parametrize("bundle", [
    {"version": 1},
    {"version": 2},
    {"version": 2, "symbol": "BTCUSDT"},
])
def test_live_rejects_unbound_or_wrong_asset_model_before_creating_run(bundle, tmp_path):
    import json

    from btc_cta.cross_arb.forecast import live

    bundle.update(venues={"bybit": {}, "bitget": {}}, sample_ms=50)
    path = tmp_path / "model.json"
    path.write_text(json.dumps(bundle))
    output = tmp_path / "rejected"
    with pytest.raises(ValueError, match="symbol|bundle"):
        live(path, output, 1, symbol="SKLUSDT")
    assert not output.exists()


@pytest.mark.parametrize("venue,payload", [
    ("bybit", {"topic": "orderbook.50.BTCUSDT", "data": {"s": "BTCUSDT"}}),
    ("bybit", {"topic": "publicTrade.SKLUSDT", "data": [{"s": "BTCUSDT"}]}),
    ("bitget", {"arg": {"symbol": "BTCUSDT"}}),
    ("bitget", {"arg": {"symbol": "SKLUSDT"}, "data": [{"symbol": "BTCUSDT"}]}),
])
def test_replay_rejects_mixed_asset_archive_instead_of_swallowing_as_parse_reset(
    venue, payload, tmp_path
):
    import gzip
    import json

    from btc_cta.cross_arb.forecast import replay

    archive = tmp_path / "mixed.jsonl.gz"
    with gzip.open(archive, "wt") as file:
        file.write(json.dumps({"venue": venue, "received_at_ms": 1000, "payload": payload}) + "\n")
    output = tmp_path / "evaluation"
    with pytest.raises(ValueError, match="symbol"):
        replay(archive, output, symbol="SKLUSDT")
    assert not (output / "model.json").exists()


def test_non_btc_archive_trains_symbol_bound_model_and_predictions(tmp_path):
    import csv
    import gzip
    import json

    from btc_cta.cross_arb.forecast import replay

    archive = tmp_path / "public-ws.jsonl.gz"
    with gzip.open(archive, "wt") as file:
        for i in range(2000):
            source = 10000 + i * 20
            bids = [[f"{0.004 + (i % 5) * 0.000001:.6f}", "100000"]]
            asks = [[f"{0.00401 + (i % 5) * 0.000001:.6f}", "100000"]]
            for venue in ("bybit", "bitget"):
                if venue == "bybit":
                    payload = {
                        "topic": "orderbook.50.SKLUSDT", "type": "snapshot", "cts": source,
                        "data": {"s": "SKLUSDT", "seq": i + 1, "u": i + 1, "b": bids, "a": asks},
                    }
                else:
                    payload = {
                        "arg": {"instType": "usdt-futures", "symbol": "SKLUSDT", "topic": "books5"},
                        "action": "snapshot",
                        "data": [{"seq": i + 1, "ts": source, "b": bids, "a": asks}],
                    }
                file.write(json.dumps({
                    "venue": venue, "received_at_ms": source + 60, "payload": payload,
                }) + "\n")
    output = tmp_path / "evaluation"
    replay(archive, output, sample_ms=20, symbol="SKLUSDT")
    model = json.loads((output / "model.json").read_text())
    assert model["version"] == 2
    assert model["symbol"] == "SKLUSDT"
    assert model["book_channels"] == {"bybit": "orderbook.50", "bitget": "books5"}
    assert model["execution_enabled"] is False
    evaluation = json.loads((output / "evaluation.json").read_text())
    assert evaluation["symbol"] == "SKLUSDT"
    assert evaluation["frames"] == 4000
    assert evaluation["parse_resets"] == {}
    for row in evaluation["venues"].values():
        assert row["test"] >= 100
    with gzip.open(output / "holdout-predictions.csv.gz", "rt") as file:
        rows = list(csv.DictReader(file))
    assert rows and all(row["symbol"] == "SKLUSDT" for row in rows)


def test_legacy_snapshot_channel_model_cannot_silently_run_on_new_depth_feed(tmp_path):
    import json

    from btc_cta.cross_arb.forecast import live

    model = {"version": 2, "symbol": "SKLUSDT", "venues": {"bybit": {}, "bitget": {}},
             "book_channels": {"bybit": "orderbook.50", "bitget": "books5"}, "sample_ms": 50}
    path = tmp_path / "model.json"
    path.write_text(json.dumps(model))
    with pytest.raises(ValueError, match="channels"):
        live(path, tmp_path / "run", 1, symbol="SKLUSDT")
    assert not (tmp_path / "run").exists()
