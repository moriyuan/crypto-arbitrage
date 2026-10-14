from __future__ import annotations

import gzip
import json

import pytest


def setup_feed(
    monkeypatch,
    *,
    events=None,
    failure=None,
    dropped=0,
    discontinuity=None,
    close_invalidations=0,
    close_delay=0,
    snapshot_only=False,
    omit_book_type=False,
):
    import btc_cta.cross_arb.capture as module
    import btc_cta.cross_arb.stream as stream

    class Clock:
        now = 0.0

    clock = Clock()
    options = {}
    messages = list(events or [])

    class Feed:
        def __init__(self, **kwargs):
            options.update(kwargs)
            self.rows = {
                venue: {"book_updates": 0, "trade_ticks": 0, "connected": False}
                for venue in ("bybit", "bitget")
            }

        def __enter__(self):
            for row in self.rows.values():
                row["connected"] = True
            return self

        def __exit__(self, *args):
            for row in self.rows.values():
                row["connected"] = False
                row["invalidations"] = row.get("invalidations", 0) + close_invalidations
            clock.now += close_delay

        def next_event(self, timeout):
            clock.now += timeout
            if messages:
                kind, venue = messages.pop(0)
                payload = {"topic": kind}
                if kind == "book" and not omit_book_type:
                    field = "type" if venue == "bybit" else "action"
                    payload[field] = (
                        "snapshot"
                        if snapshot_only is True or snapshot_only == venue
                        else ("delta" if venue == "bybit" else "update")
                    )
                row = {"kind": kind, "venue": venue, "payload": payload}
                options["on_message"](venue, int(clock.now * 1000), row["payload"])
                if kind == "book":
                    self.rows[venue]["book_updates"] += 1
                elif kind == "trade":
                    self.rows[venue]["trade_ticks"] += 1
                return row
            if discontinuity:
                self.rows["bybit"][discontinuity] = 1
            if failure:
                raise failure("sensitive server response must not be copied")
            return None

        def stats(self):
            return {
                **{venue: row.copy() for venue, row in self.rows.items()},
                "symbol": options["symbol"],
                "fatal_error": None,
                "dropped_events": dropped,
                "dropped_updates": 0,
                "purged_events": 0,
            }

    monkeypatch.setattr(stream, "StreamBooks", Feed)
    monkeypatch.setattr(module.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(module.time, "time", lambda: 1000000 + clock.now)
    return module, options


COMPLETE_EVENTS = [(kind, venue) for kind in ("book", "trade") for venue in ("bybit", "bitget")]


def test_capture_passes_symbol_records_closed_archive_and_heartbeat(monkeypatch, tmp_path):
    module, options = setup_feed(monkeypatch, events=COMPLETE_EVENTS)
    snapshots = []
    write = module.write_json

    def save(path, value):
        if path.name == "status.json":
            snapshots.append(json.loads(json.dumps(value)))
        write(path, value)

    monkeypatch.setattr(module, "write_json", save)
    output = tmp_path / "run"
    assert module.run("ALTUSDT", output, 5.0, None) == 0
    status = json.loads((output / "status.json").read_text())
    metadata = json.loads((output / "run_metadata.json").read_text())
    assert options["symbol"] == "ALTUSDT"
    assert options["event_mode"] is True
    assert options["bitget_depth_channel"] == "books"
    assert metadata["symbol"] == "ALTUSDT"
    assert metadata["mode"] == "order_flow_collection"
    assert metadata["source"]["bitget"] == "publicTrade + books"
    assert metadata["book_channels"] == {"bybit": "orderbook.50", "bitget": "books"}
    assert metadata["execution_enabled"] is False
    assert status["status"] == "COMPLETED"
    assert status["elapsed_seconds"] == pytest.approx(5)
    assert status["raw_frames"] == 4
    assert status["book_message_types"]["bybit"]["delta"] == 1
    assert status["book_message_types"]["bitget"]["update"] == 1
    assert status["stream"]["bybit"]["connected"] is False
    beats = [row["heartbeat_ms"] for row in snapshots if row["status"] == "RUNNING"]
    assert len(beats) >= 3
    assert all(0 <= b - a <= 2000 for a, b in zip(beats, beats[1:], strict=False))
    with gzip.open(output / "public-ws.jsonl.gz", "rt") as file:
        rows = [json.loads(line) for line in file]
    assert len(rows) == 4
    assert not (output / "model.json").exists()


def test_capture_no_paired_trade_data_is_not_success(monkeypatch, tmp_path):
    module, _ = setup_feed(monkeypatch, events=[("book", "bybit"), ("book", "bitget")])
    assert module.run("ALTUSDT", tmp_path / "run", 1, None) == 1
    status = json.loads((tmp_path / "run/status.json").read_text())
    assert status["status"] == "NO_PAIRED_DATA"
    assert status["execution_enabled"] is False
    assert status["raw_frames"] == 2


@pytest.mark.parametrize(
    "failure,expected,code", [(RuntimeError, "FAILED", 1), (KeyboardInterrupt, "STOPPED", 130)]
)
def test_capture_exception_preserves_valid_gzip_and_safe_error(
    monkeypatch, tmp_path, failure, expected, code
):
    module, _ = setup_feed(monkeypatch, events=[("book", "bybit")], failure=failure)
    output = tmp_path / "run"
    assert module.run("ALTUSDT", output, 5, None) == code
    status = json.loads((output / "status.json").read_text())
    assert status["status"] == expected
    assert status["error"] == failure.__name__
    assert "sensitive" not in (output / "status.json").read_text()
    with gzip.open(output / "public-ws.jsonl.gz", "rt") as file:
        assert len(list(file)) == 1


def test_capture_queue_loss_rejects_completion(monkeypatch, tmp_path):
    module, _ = setup_feed(monkeypatch, events=COMPLETE_EVENTS, dropped=1)
    assert module.run("ALTUSDT", tmp_path / "run", 2, None) == 1
    status = json.loads((tmp_path / "run/status.json").read_text())
    assert status["status"] == "FAILED"
    assert status["error"] == "stream_queue_loss"


@pytest.mark.parametrize("seconds", [0, -1, 1801, float("nan"), float("inf")])
def test_capture_invalid_duration_creates_nothing(tmp_path, seconds):
    from btc_cta.cross_arb.capture import run

    with pytest.raises(ValueError):
        run("ALTUSDT", tmp_path / "run", seconds, None)
    assert not (tmp_path / "run").exists()


@pytest.mark.parametrize("failed", [False, True])
def test_evaluation_only_after_complete_capture_and_bound_to_same_symbol(
    monkeypatch, tmp_path, failed
):
    import btc_cta.cross_arb.forecast as forecast

    module, _ = setup_feed(monkeypatch, events=COMPLETE_EVENTS)
    calls = []

    def replay(archive, output, *, symbol):
        calls.append((archive, output, symbol))
        assert json.loads((output.parent / "status.json").read_text())["status"] == "EVALUATING"
        with gzip.open(archive, "rt") as file:
            assert len(list(file)) == 4
        if failed:
            raise ValueError("insufficient causal labels")
        output.mkdir()
        (output / "model.json").write_text(json.dumps({"symbol": symbol}))

    monkeypatch.setattr(forecast, "replay", replay)
    output = tmp_path / "run"
    assert module.run("ALTUSDT", output, 2, None, evaluate=True) == int(failed)
    status = json.loads((output / "status.json").read_text())
    assert status["status"] == ("EVALUATION_FAILED" if failed else "COMPLETED")
    assert len(calls) == 1
    assert calls[0][2] == "ALTUSDT"
    assert status["execution_enabled"] is False
    if failed:
        assert status["evaluation_error"] == "ValueError"
        assert not (output / "evaluation/model.json").exists()


def test_cli_requires_explicit_symbol(monkeypatch):
    from btc_cta.cross_arb.capture import main

    monkeypatch.setattr("sys.argv", ["capture", "--output", "/tmp/not-created-by-test"])
    with pytest.raises(SystemExit) as raised:
        main()
    assert raised.value.code == 2


def test_incomplete_capture_never_calls_evaluation(monkeypatch, tmp_path):
    import btc_cta.cross_arb.forecast as forecast

    module, _ = setup_feed(monkeypatch)

    def forbidden(*args, **kwargs):
        raise AssertionError("evaluation must not receive an incomplete dataset")

    monkeypatch.setattr(forecast, "replay", forbidden)
    assert module.run("ALTUSDT", tmp_path / "run", 1, None, evaluate=True) == 1
    assert not (tmp_path / "run/evaluation").exists()


def test_existing_output_is_preserved(tmp_path):
    from btc_cta.cross_arb.capture import run

    output = tmp_path / "run"
    output.mkdir()
    (output / "status.json").write_text("keep this prior evidence")
    with pytest.raises(FileExistsError):
        run("ALTUSDT", output, 1, None)
    assert (output / "status.json").read_text() == "keep this prior evidence"


def test_invalid_symbol_creates_nothing(tmp_path):
    from btc_cta.cross_arb.capture import run

    with pytest.raises(ValueError):
        run("../../BTCUSDT", tmp_path / "run", 1, None)
    assert not (tmp_path / "run").exists()


def test_stop_during_evaluation_is_not_reported_complete(monkeypatch, tmp_path):
    import signal

    import btc_cta.cross_arb.forecast as forecast

    module, _ = setup_feed(monkeypatch, events=COMPLETE_EVENTS)
    prior_handler = signal.getsignal(signal.SIGTERM)

    def replay(*args, **kwargs):
        handler = signal.getsignal(signal.SIGTERM)
        handler(signal.SIGTERM, None)

    monkeypatch.setattr(forecast, "replay", replay)
    output = tmp_path / "run"
    assert module.run("ALTUSDT", output, 2, None, evaluate=True) == 130
    status = json.loads((output / "status.json").read_text())
    assert status["status"] == "STOPPED"
    assert status["collection_status"] == "COMPLETED"
    assert signal.getsignal(signal.SIGTERM) is prior_handler


@pytest.mark.parametrize("discontinuity", ["disconnects", "invalidations", "event"])
def test_disconnect_without_queue_purge_preserves_data_and_blocks_evaluation(
    monkeypatch, tmp_path, discontinuity
):
    import btc_cta.cross_arb.forecast as forecast

    events = COMPLETE_EVENTS + ([("invalidate", "bybit")] if discontinuity == "event" else [])
    module, _ = setup_feed(
        monkeypatch,
        events=events,
        discontinuity=discontinuity if discontinuity != "event" else None,
    )
    replay_calls = []
    monkeypatch.setattr(forecast, "replay", lambda *a, **kw: replay_calls.append(a))
    output = tmp_path / "run"
    assert module.run("ALTUSDT", output, 2, None, evaluate=True) == 1
    status = json.loads((output / "status.json").read_text())
    assert status["status"] == "FAILED"
    assert status["error"] == "stream_discontinuity"
    assert status["stream"]["purged_events"] == 0
    assert replay_calls == []
    with gzip.open(output / "public-ws.jsonl.gz", "rt") as file:
        assert len(list(file)) >= 4


def test_normal_close_invalidations_do_not_taint_completed_collection(monkeypatch, tmp_path):
    import btc_cta.cross_arb.forecast as forecast

    module, _ = setup_feed(
        monkeypatch, events=COMPLETE_EVENTS, close_invalidations=1, close_delay=0.5
    )
    replay_calls = []
    monkeypatch.setattr(forecast, "replay", lambda *a, **kw: replay_calls.append(a))
    output = tmp_path / "run"
    assert module.run("ALTUSDT", output, 2, None, evaluate=True) == 0
    status = json.loads((output / "status.json").read_text())
    assert status["status"] == "COMPLETED"
    assert status["stream"]["bybit"]["invalidations"] == 1
    assert status["stream_before_close"]["bybit"].get("invalidations", 0) == 0
    assert status["collection_elapsed_seconds"] == pytest.approx(2)
    assert status["elapsed_seconds"] == pytest.approx(2.5)
    assert len(replay_calls) == 1


@pytest.mark.parametrize("snapshot_only", [True, "bybit", "bitget"])
def test_snapshot_only_books_do_not_satisfy_incremental_collection(
    monkeypatch, tmp_path, snapshot_only
):
    import btc_cta.cross_arb.forecast as forecast

    module, _ = setup_feed(monkeypatch, events=COMPLETE_EVENTS, snapshot_only=snapshot_only)
    replay_calls = []
    monkeypatch.setattr(forecast, "replay", lambda *a, **kw: replay_calls.append(a))
    output = tmp_path / "run"
    assert module.run("ALTUSDT", output, 2, None, evaluate=True) == 1
    status = json.loads((output / "status.json").read_text())
    assert status["status"] == "NO_INCREMENTAL_DATA"
    assert status["collection_status"] == "NO_INCREMENTAL_DATA"
    for venue, kind in (("bybit", "delta"), ("bitget", "update")):
        snapshots = int(snapshot_only is True or snapshot_only == venue)
        assert status["book_message_types"][venue] == {"snapshot": snapshots, kind: 1 - snapshots}
    assert replay_calls == []
    with gzip.open(output / "public-ws.jsonl.gz", "rt") as file:
        assert len(list(file)) == 4


def test_book_payload_without_message_type_is_ignored_and_cannot_enable_training(
    monkeypatch, tmp_path
):
    module, _ = setup_feed(monkeypatch, events=COMPLETE_EVENTS, omit_book_type=True)
    output = tmp_path / "run"
    assert module.run("ALTUSDT", output, 2, None) == 1
    status = json.loads((output / "status.json").read_text())
    assert status["status"] == "NO_INCREMENTAL_DATA"
    assert status["book_message_types"] == {
        "bybit": {"snapshot": 0, "delta": 0},
        "bitget": {"snapshot": 0, "update": 0},
    }
