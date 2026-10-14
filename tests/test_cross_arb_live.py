from __future__ import annotations

import gzip
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from btc_cta.cross_arb.live import RawRecorder, funding_assessment, write_json


def test_raw_recorder_keeps_batched_trades_and_concurrent_frames(tmp_path: Path) -> None:
    path = tmp_path / "raw.jsonl.gz"
    with RawRecorder(path) as writer:
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(
                pool.map(
                    lambda n: writer(
                        "bybit", 1000 + n, {"data": [{"i": str(n)}, {"i": str(n + 1)}]}
                    ),
                    range(20),
                )
            )
        assert writer.count == 20
    with gzip.open(path, "rt") as inp:
        rows = [json.loads(line) for line in inp]
    assert len(rows) == 20
    assert {r["received_at_ms"] for r in rows} == set(range(1000, 1020))
    assert all(len(r["payload"]["data"]) == 2 for r in rows)
    with pytest.raises(FileExistsError):
        RawRecorder(path)


def test_atomic_status_replaces_existing_valid_json(tmp_path: Path) -> None:
    path = tmp_path / "status.json"
    write_json(path, {"status": "RUNNING"})
    write_json(path, {"status": "COMPLETED"})
    assert json.loads(path.read_text()) == {"status": "COMPLETED"}


def test_funding_assessment_requires_pre_entry_schedule_for_both_venues() -> None:
    cycle = {"entry_timestamp_ms": 1000, "exit_timestamp_ms": 2000}
    schedule = {v: {"next_ms": 3000, "fetched_ms": 900, "error": None} for v in ("bybit", "bitget")}
    assert funding_assessment([cycle], [schedule], None)["funding_usdt"] == "0"
    assert funding_assessment([cycle], [], None)["funding_usdt"] is None
    schedule["bitget"]["next_ms"] = 1500
    assert funding_assessment([cycle], [schedule], None)["status"] == "UNKNOWN"


def test_funding_no_closed_cycle_but_open_position_is_unresolved() -> None:
    result = funding_assessment([], [], {"entry_timestamp_ms": 1000})
    assert result["status"] == "UNRESOLVED"
    assert result["funding_usdt"] is None


def test_no_trades_have_no_funding_charge() -> None:
    assert funding_assessment([], [], None)["funding_usdt"] == "0"


def test_funding_schedule_changed_inside_position_cannot_claim_zero() -> None:
    cycle = {"entry_timestamp_ms": 1_000_000, "exit_timestamp_ms": 1_040_000}
    initial = {
        v: {"next_ms": 4_600_000, "fetched_ms": 1_000_000, "error": None}
        for v in ("bybit", "bitget")
    }
    later = {
        v: {"next_ms": 1_030_000, "fetched_ms": 1_010_000, "error": None}
        for v in ("bybit", "bitget")
    }
    assert funding_assessment([cycle], [initial, later], None)["status"] == "UNKNOWN"


@pytest.mark.parametrize("filters_enabled,reconnect,wall_jump,jump_tick", [
    (False, False, 0, 0), (True, False, 0, 0), (True, True, 0, 0),
    (False, False, 3600, 1), (False, False, 3600, 2),
    (False, False, -3600, 1), (False, False, -3600, 2), (False, False, 2, 1),
])
def test_direct_taker_run_binds_skl_and_uses_actual_books_without_model(
    monkeypatch, tmp_path, filters_enabled, reconnect, wall_jump, jump_tick
):
    import sqlite3
    from decimal import Decimal as D

    import btc_cta.cross_arb.funding as funding
    import btc_cta.cross_arb.live as live
    import btc_cta.cross_arb.market as market
    import btc_cta.cross_arb.stream as stream

    clock = [100.0]
    wall_offset = [0.0]
    seen = {}
    venues = ("bybit", "bitget")
    monkeypatch.setattr(live.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(live.time, "time", lambda: 1791480000 + clock[0] + wall_offset[0])
    monkeypatch.setattr(live.signal, "signal", lambda *args: None)

    class PublicRules:
        def __init__(self, **kwargs):
            seen["market"] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def fetch_rules(self):
            return {v: market.Rules(D(1), D(1), D(5), D(".000001")) for v in venues}

    class PublicFunding(PublicRules):
        def __init__(self, **kwargs):
            seen["funding"] = kwargs

        def refresh(self, now):
            return {v: {"next_ms": now + 14400000, "fetched_ms": now, "error": None}
                    for v in venues}

        def decision(self, *args):
            return None

    class IncrementalBooks(PublicRules):
        def __init__(self, **kwargs):
            seen["stream"] = {k: v for k, v in kwargs.items() if k != "on_message"}
            self.record = kwargs["on_message"]
            self.n = 0
            self.closed = False

        def __exit__(self, *args):
            self.closed = True

        def snapshot(self):
            now = int(live.time.time() * 1000)
            prices = {"bybit": (".004999", ".005000"), "bitget": (".005050", ".005051")}
            if self.n >= 2:
                prices = dict.fromkeys(venues, (".005024", ".005025"))
            scale = D("1.001") if reconnect and self.n % 2 else D(1)
            return {v: market.Book(v, ((D(b) * scale, D(100000)),),
                                   ((D(a) * scale, D(100000)),),
                                   now, now, 0) for v, (b, a) in prices.items()}

        def next_update(self, **kwargs):
            clock[0] += 0.25
            self.n += 1
            if self.n == jump_tick:
                wall_offset[0] += wall_jump
            books = self.snapshot()
            for v in venues:
                self.record(v, books[v].received_at_ms, {"symbol": "SKLUSDT", "sequence": self.n})
            return books

        def stats(self):
            return {
                "symbol": "SKLUSDT", "book_channels": {"bybit": "orderbook.50", "bitget": "books"},
                "fatal_error": None, "dropped_updates": 0,
                **{v: {"connected": not self.closed, "frames": self.n,
                       "disconnects": int(reconnect and self.n >= 11),
                       "invalidations": int(self.closed) + int(reconnect and self.n >= 11)}
                   for v in venues},
            }

    monkeypatch.setattr(market, "MarketData", PublicRules)
    monkeypatch.setattr(funding, "FundingGuard", PublicFunding)
    monkeypatch.setattr(stream, "StreamBooks", IncrementalBooks)
    out = tmp_path / "skl"
    kwargs = {} if filters_enabled else {"entry_filter": None}
    if reconnect:
        kwargs["entry_filter"] = live.EntryFilterConfig(
            volatility_window_s=2, volatility_sample_ms=250
        )
    result = live.run(out, 3 if reconnect else 1, None, symbol="SKLUSDT", **kwargs)
    assert result == (2 if abs(wall_jump) > 5 else 0)
    status = json.loads((out / "status.json").read_text())
    metadata = json.loads((out / "run_metadata.json").read_text())
    assert metadata["symbol"] == "SKLUSDT"
    assert metadata["policy"]["bybit_fee_bps"] == "3"
    assert metadata["policy"]["bitget_fee_bps"] == "3"
    assert metadata["model_used"] is False
    assert metadata["signal_model"] == "confirmed_depth_taker"
    assert metadata["execution_enabled"] is False
    assert seen["market"]["symbol"] == seen["funding"]["symbol"] == "SKLUSDT"
    assert seen["stream"]["symbol"] == "SKLUSDT"
    assert seen["stream"]["bitget_depth_channel"] == "books"
    if wall_jump:
        assert status["status"] == ("FAILED" if abs(wall_jump) > 5 else "COMPLETED")
        assert status["error"] == ("clock_discontinuity" if abs(wall_jump) > 5 else None)
        backward_open = wall_jump < 0 and jump_tick == 2
        assert status["ledger"]["orders"] == (2 if backward_open else 4 if jump_tick == 2 else 0)
        assert (status["ledger"]["cycle"] is not None) == backward_open
        if not backward_open:
            assert status["ledger"]["stop_requested"] is True
        with sqlite3.connect(out / "ledger.sqlite") as db:
            orders = [json.loads(r[0]) for r in db.execute("SELECT value FROM orders")]
        started_ms = int((1791480000 + 100) * 1000)
        assert all(o["timestamp_ms"] < started_ms + 1000 for o in orders if o["intent"] == "entry")
        return
    assert status["status"] == "COMPLETED"
    assert status["funding"]["status"] == "VERIFIED_NO_SETTLEMENT"
    if filters_enabled:
        assert metadata["entry_filter"]["min_volatility_bps"] == "5"
        assert metadata["entry_filter"]["min_depth_usdt"] == "500"
        assert status["ledger"]["entry_filter"] == metadata["entry_filter"]
        assert status["ledger"]["orders"] == 0
        assert status["ledger"]["completed_cycles"] == 0
        with sqlite3.connect(out / "ledger.sqlite") as db:
            signals = [json.loads(r[0]) for r in db.execute("SELECT value FROM signals")]
        if reconnect:
            reset_at = int((1791480000 + 102.75) * 1000)
            assert any(s["entry_filter_passed"] for s in signals if s["timestamp_ms"] < reset_at)
            assert all(
                not s["entry_filter_passed"] for s in signals if s["timestamp_ms"] >= reset_at
            )
            assert all(
                s["volatility_samples"] == 0 for s in signals if s["timestamp_ms"] == reset_at
            )
        else:
            assert all(not s["entry_filter_passed"] for s in signals)
        assert any(s["entry_filter_reason"] == "entry_filter_warmup" for s in signals)
        return
    assert status["ledger"]["completed_cycles"] == 1
    assert status["ledger"]["orders"] == 4
    assert status["ledger"]["cycle"] is None
    assert status["stream_before_close"]["bybit"]["connected"] is True
    assert status["stream"]["bybit"]["connected"] is False
    with sqlite3.connect(out / "ledger.sqlite") as db:
        orders = [json.loads(r[0]) for r in db.execute("SELECT value FROM orders")]
        cycles = [json.loads(r[0]) for r in db.execute("SELECT value FROM cycles")]
    assert {r["pair"] for r in orders} == {"SKL/USDT:USDT"}
    assert len({r["qty"] for r in orders}) == 1
    assert all(D(r["fee_rate"]) == D(".0003") for r in orders)
    assert all(D(r["fee"]) == D(r["notional"]) * D(".0003") for r in orders)
    assert [r["price"] for r in orders] == ["0.005000", "0.005050", "0.005024", "0.005025"]
    assert sum(D(x) for x in status["ledger"]["cash"].values()) == (
        D(2000) + D(cycles[0]["net_before_funding"])
    )
    assert "SKLUSDT" in (out / "report.md").read_text()
    assert "BTC/USDT" not in (out / "report.md").read_text()
    assert "Bybit 3 bp，Bitget 3 bp" in (out / "report.md").read_text()
    assert not (out / "model.json").exists()


@pytest.mark.parametrize("symbol", ["BTC/USDT", "SKLUSDT;", "sklusdt", "USDT"])
def test_live_rejects_invalid_symbol_before_creating_output(tmp_path, symbol):
    from btc_cta.cross_arb.live import run

    output = tmp_path / "invalid"
    with pytest.raises(ValueError):
        run(output, 1, None, symbol=symbol)
    assert not output.exists()


@pytest.mark.parametrize("value", ["-1", "NaN", "Infinity", "101"])
def test_live_invalid_fee_rejected_before_creating_run(tmp_path, value):
    from decimal import Decimal

    from btc_cta.cross_arb.live import run

    out = tmp_path / "bad_fee"
    with pytest.raises(ValueError, match="fee_bps"):
        run(out, 1, None, symbol="SKLUSDT", fee_bps=Decimal(value))
    assert not out.exists()


@pytest.mark.parametrize("arguments,expected", [([], "3"), (["--fee-bps", "6"], "6")])
def test_live_cli_fee_default_and_explicit_override(monkeypatch, tmp_path, arguments, expected):
    from decimal import Decimal

    import btc_cta.cross_arb.live as live

    seen = {}

    def run(*args, **kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(live, "run", run)
    monkeypatch.setattr("sys.argv", ["live", "--symbol", "SKLUSDT", "--output", str(tmp_path),
                                    "--seconds", "1", *arguments])
    with pytest.raises(SystemExit) as exc:
        live.main()
    assert exc.value.code == 0
    assert seen["fee_bps"] == Decimal(expected)
    assert seen["symbol"] == "SKLUSDT"


def test_live_cli_exposes_searchable_entry_filters(monkeypatch, tmp_path):
    from decimal import Decimal

    import btc_cta.cross_arb.live as live

    seen = {}
    monkeypatch.setattr(live, "run", lambda *a, **k: seen.update(k) or 0)
    monkeypatch.setattr("sys.argv", ["live", "--symbol", "SKLUSDT", "--output", str(tmp_path),
                                   "--volatility-window-s", "30", "--volatility-sample-ms", "500",
                                   "--min-volatility-bps", "8", "--liquidity-band-bps", "15",
                                   "--min-depth-usdt", "750"])
    with pytest.raises(SystemExit) as exc:
        live.main()
    assert exc.value.code == 0
    c = seen["entry_filter"]
    assert (c.volatility_window_s, c.volatility_sample_ms) == (30, 500)
    assert (c.min_volatility_bps, c.liquidity_band_bps, c.min_depth_usdt) == tuple(
        map(Decimal, ("8", "15", "750"))
    )


@pytest.mark.parametrize("args", [["--min-volatility-bps", "NaN"],
                                 ["--min-depth-usdt", "-1"],
                                 ["--volatility-window-s", "0"]])
def test_bad_filter_cli_never_starts_or_creates_output(monkeypatch, tmp_path, args):
    import btc_cta.cross_arb.live as live

    seen = []
    out = tmp_path / "not-created"
    monkeypatch.setattr(live, "run", lambda *a, **k: seen.append(k))
    monkeypatch.setattr("sys.argv", ["live", "--symbol", "SKLUSDT", "--output", str(out), *args])
    with pytest.raises(SystemExit) as exc:
        live.main()
    assert exc.value.code == 2
    assert seen == [] and not out.exists()


@pytest.mark.parametrize("wall_delta,mono_delta,expected", [
    (9.9, 9.9, None), (10, 10, "session_end"),
    (11, 9, "session_end"), (9, 11, "session_end"),
    (3600, .25, "clock_discontinuity"), (-20, .25, "clock_discontinuity"),
])
def test_run_clock_stops_on_either_deadline_or_discontinuity(wall_delta, mono_delta, expected):
    from btc_cta.cross_arb.live import _run_clock_reason

    assert _run_clock_reason(100000, 10.0, 10.0, int(100000 + wall_delta * 1000),
                             10.0 + mono_delta) == expected
