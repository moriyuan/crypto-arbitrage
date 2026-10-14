from __future__ import annotations

import json
from decimal import Decimal

import pytest

from btc_cta.cross_arb.market import Book, MarketDataError
from btc_cta.cross_arb.stream import SUBSCRIPTIONS, URLS, StreamBooks, _BookParser


def bybit(kind="snapshot", seq=10, update_id=20, ts=1000, bids=None, asks=None):
    return {"topic": "orderbook.50.BTCUSDT", "type": kind, "cts": ts,
            "data": {"s": "BTCUSDT", "seq": seq, "u": update_id,
                     "b": [["100", "2"]] if bids is None else bids,
                     "a": [["101", "3"]] if asks is None else asks}}


def bitget(seq=10, ts="1000", bids=None, asks=None):
    return {"arg": {"instType": "usdt-futures", "topic": "books5", "symbol": "BTCUSDT"},
            "action": "snapshot", "data": [{"seq": seq, "ts": ts,
                "b": [["100", "2"]] if bids is None else bids,
                "a": [["101", "3"]] if asks is None else asks}]}


def test_bybit_snapshot_delta_updates_delete_insert_and_noncontiguous_ids():
    parser = _BookParser("bybit")
    first = parser.parse(bybit(bids=[["100", "2"], ["99", "4"]]), 1100)
    assert first is not None and first.exchange_ts_ms == 1000
    result = parser.parse(bybit("delta", 15, 29, 1001, [["100", "0"], ["98", "3"]],
                               [["101", "2"]]), 1101)
    assert result is not None
    assert result.bids == ((Decimal("99"), Decimal("4")), (Decimal("98"), Decimal("3")))
    assert result.asks == ((Decimal("101"), Decimal("2")),)
    assert first.bids[0][0] == Decimal("100")


@pytest.mark.parametrize("kind,seq,update_id", [("snapshot", 2, 3), ("delta", 1, 1)])
def test_bybit_snapshot_and_service_restart_replace_book(kind, seq, update_id):
    parser = _BookParser("bybit")
    parser.parse(bybit(), 1100)
    result = parser.parse(bybit(kind, seq, update_id, 1001, [["99", "2"]]), 1101)
    assert result is not None and result.bids == ((Decimal("99"), Decimal("2")),)


def test_delta_requires_snapshot():
    with pytest.raises(MarketDataError, match="before snapshot"):
        _BookParser("bybit").parse(bybit("delta"), 1100)


@pytest.mark.parametrize("seq,update_id,ts", [(9, 21, 1001), (10, 21, 1001),
                                              (11, 20, 1001), (11, 21, 999)])
def test_out_of_order_bybit_fails_closed(seq, update_id, ts):
    parser = _BookParser("bybit")
    parser.parse(bybit(), 1100)
    with pytest.raises(MarketDataError):
        parser.parse(bybit("delta", seq, update_id, ts), 1101)


def test_bitget_snapshots_replace_not_merge():
    parser = _BookParser("bitget")
    parser.parse(bitget(), 1100)
    result = parser.parse(bitget(11, "1001", [["99", "1"]]), 1101)
    assert result is not None and result.bids == ((Decimal("99"), Decimal("1")),)


@pytest.mark.parametrize("seq,ts", [(9, "1001"), (10, "1001"), (11, "999")])
def test_out_of_order_bitget_fails_closed(seq, ts):
    parser = _BookParser("bitget")
    parser.parse(bitget(), 1100)
    with pytest.raises(MarketDataError):
        parser.parse(bitget(seq, ts), 1101)


@pytest.mark.parametrize("mutate", [
    lambda p: p.update(action="update"),
    lambda p: p["arg"].update(symbol="ETHUSDT"),
    lambda p: p["arg"].update(instType="spot"),
    lambda p: p["data"][0].update(b=[["102", "1"]]),
    lambda p: p["data"][0].update(b=[["100", "0"]]),
    lambda p: p["data"][0].update(b=[["100", "NaN"]]),
    lambda p: p["data"][0].update(b=[["100", "1"], ["100", "2"]]),
    lambda p: p["data"][0].update(b=[[str(i), "1"] for i in range(100, 93, -1)]),
])
def test_bitget_invalid_books_rejected(mutate):
    payload = bitget()
    mutate(payload)
    with pytest.raises(MarketDataError):
        _BookParser("bitget").parse(payload, 1100)


def trade_payload(venue):
    trades = [{"i": "a", "T": "1000", "p": "100", "v": "0.001",
               "S": "Buy" if venue == "bybit" else "buy", "s": "BTCUSDT"},
              {"i": "b", "T": "1000", "p": "101", "v": "0.002",
               "S": "Sell" if venue == "bybit" else "sell", "s": "BTCUSDT"}]
    if venue == "bybit":
        return {"topic": "publicTrade.BTCUSDT", "data": trades}
    return {"arg": {"instType": "usdt-futures", "topic": "publicTrade", "symbol": "BTCUSDT"},
            "data": trades}


@pytest.mark.parametrize("venue", ["bybit", "bitget"])
def test_raw_trade_callback_preserves_every_id_and_does_not_update_book(venue):
    messages = []
    stream = StreamBooks(on_message=lambda v, t, p: messages.append((v, t, p)))
    payload = trade_payload(venue)
    stream._message(venue, _BookParser(venue), json.dumps(payload))
    assert messages[0][0] == venue and messages[0][2] == payload
    assert [t["i"] for t in messages[0][2]["data"]] == ["a", "b"]
    assert stream.stats()[venue]["trade_ticks"] == 2
    assert stream.snapshot() is None


def fill_stream(stream):
    for venue, payload in (("bybit", bybit()), ("bitget", bitget())):
        stream._message(venue, _BookParser(venue), json.dumps(payload))


def test_stream_fifo_each_update_then_disconnect_clears_all_queued():
    stream = StreamBooks()
    fill_stream(stream)
    first = stream.next_update(timeout=0)
    assert first is not None and set(first) == {"bybit", "bitget"}
    book = Book("bybit", ((Decimal("99"), Decimal("2")),),
                ((Decimal("101"), Decimal("3")),), 1200, 1199, 0)
    stream._publish("bybit", book)
    assert stream.stats()["queued_updates"] == 1
    stream._invalidate("bitget", "ConnectionClosed")
    assert stream.next_update(timeout=0) is None
    assert stream.snapshot() is None
    assert stream.stats()["bitget"]["disconnects"] == 1


def test_queue_overflow_is_fatal_not_silent_loss():
    stream = StreamBooks(max_updates=1)
    fill_stream(stream)
    book = stream.snapshot()["bybit"]
    stream._publish("bybit", book)
    assert stream.stats()["fatal_error"] == "update_queue_overflow"
    assert stream.stats()["dropped_updates"] == 1
    assert stream.snapshot() is None and stream.next_update(0) is None


def test_callback_failure_stops_both_feeds_and_invalidates():
    stream = StreamBooks()
    fill_stream(stream)
    def fail(*args):
        raise OSError("private arbitrary detail must never reach stats")
    stream._on_message = fail
    stream._message("bybit", _BookParser("bybit"), json.dumps(bybit()))
    assert stream.stats()["fatal_error"] == "callback_failed"
    assert "private" not in json.dumps(stream.stats())
    assert stream.snapshot() is None


def test_subscriptions_are_public_fixed_and_explicit_proxy():
    assert URLS["bybit"] == "wss://stream.bybit.com/v5/public/linear"
    assert URLS["bitget"] == "wss://ws.bitget.com/v3/ws/public"
    assert SUBSCRIPTIONS["bybit"]["args"] == ["orderbook.50.BTCUSDT", "publicTrade.BTCUSDT"]
    assert {x["topic"] for x in SUBSCRIPTIONS["bitget"]["args"]} == {"books50", "publicTrade"}
    assert StreamBooks(proxy=None).proxy is None
    with pytest.raises(MarketDataError):
        StreamBooks(proxy="http://user:secret@external.example:8080")


def test_close_invalidates_and_cannot_restart():
    stream = StreamBooks()
    fill_stream(stream)
    stream.close()
    assert stream.snapshot() is None and stream.next_update(0) is None
    with pytest.raises(RuntimeError):
        stream.__enter__()


@pytest.mark.parametrize("failure", ["network", "parser"])
def test_receiver_invalidates_before_close_handshake(monkeypatch, failure):
    from btc_cta.cross_arb import stream as module
    stream = StreamBooks(proxy=None)
    other_book = _BookParser("bitget").parse(bitget(), 1100)
    assert other_book is not None
    stream._publish("bitget", other_book)

    class FakeConnection:
        calls = 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            # A real close handshake can take seconds; no pair may remain tradable.
            assert stream.snapshot() is None
            assert stream.stats()["queued_updates"] == 0
            stream._stop.set()

        def send(self, value):
            assert json.loads(value)["op"] == "subscribe"

        def recv(self, *, timeout):
            self.calls += 1
            if self.calls == 1:
                return json.dumps(bybit())
            if failure == "network":
                raise OSError("disconnected")
            return json.dumps(bybit("delta", seq=9))

    monkeypatch.setattr(module, "connect", lambda *args, **kwargs: FakeConnection())
    stream._run("bybit")
    assert stream.stats()["bybit"]["disconnects"] == 1
    assert not stream.stats()["bybit"]["connected"]


def bitget50(action="snapshot", seq=10, pseq=0, ts="1000", bids=None, asks=None):
    payload = bitget(seq, ts, bids, asks)
    payload["arg"]["topic"] = "books50"
    payload["action"] = action
    payload["data"][0]["pseq"] = pseq
    return payload


def test_bitget50_reconstructs_absolute_size_and_first_overlapping_update():
    parser = _BookParser("bitget")
    parser.parse(bitget50(bids=[["100", "2"], ["99", "4"]]), 1100)
    result = parser.parse(bitget50("update", 12, 8, "1001",
                                 [["100", "0"], ["98", "3"]], [["101", "2"]]), 1101)
    assert result is not None
    assert result.bids == ((Decimal("99"), Decimal("4")), (Decimal("98"), Decimal("3")))
    assert result.asks == ((Decimal("101"), Decimal("2")),)
    result = parser.parse(bitget50("update", 13, 12, "1002", [], [["101", "5"]]), 1102)
    assert result is not None and result.asks[0][1] == Decimal("5")


@pytest.mark.parametrize("seq,pseq", [(9, 8), (12, 11), (10, 10)])
def test_bitget50_first_update_gap_or_nonincreasing_range_fails(seq, pseq):
    parser = _BookParser("bitget")
    parser.parse(bitget50(), 1100)
    with pytest.raises(MarketDataError):
        parser.parse(bitget50("update", seq, pseq, "1001"), 1101)


def test_bitget50_snapshot_equal_first_update_end_is_valid():
    parser = _BookParser("bitget")
    parser.parse(bitget50(), 1100)
    assert parser.parse(bitget50("update", 10, 9, "1001"), 1101) is not None
    assert parser.parse(bitget50("update", 11, 10, "1002"), 1102) is not None


@pytest.mark.parametrize("seq,pseq", [(15, 13), (15, 11), (12, 12), (1, 0)])
def test_bitget50_following_update_requires_exact_chain(seq, pseq):
    parser = _BookParser("bitget")
    parser.parse(bitget50(), 1100)
    parser.parse(bitget50("update", 12, 9, "1001"), 1101)
    with pytest.raises(MarketDataError):
        parser.parse(bitget50("update", seq, pseq, "1002"), 1102)
    assert parser.seq == 12


def test_bitget50_update_before_snapshot_fails_and_snapshot_resets_sequence():
    parser = _BookParser("bitget")
    with pytest.raises(MarketDataError, match="before snapshot"):
        parser.parse(bitget50("update", 12, 9), 1100)
    parser.parse(bitget50(), 1100)
    parser.parse(bitget50("update", 12, 9, "1001"), 1101)
    reset = parser.parse(bitget50("snapshot", 2, 0, "1002", [["99", "1"]]), 1102)
    assert reset is not None and reset.bids == ((Decimal("99"), Decimal("1")),)
    assert parser.parse(bitget50("update", 4, 1, "1003"), 1103) is not None


def test_event_queue_preserves_all_trades_and_books_without_old_update_queue():
    stream = StreamBooks(event_mode=True, max_updates=4)
    fill_stream(stream)
    payload = trade_payload("bybit")
    before = stream.snapshot()["bybit"]
    stream._message("bybit", _BookParser("bybit"), json.dumps(payload))
    events = [stream.next_event(0) for _ in range(3)]
    assert [(e["venue"], e["kind"]) for e in events] == [
        ("bybit", "book"), ("bitget", "book"), ("bybit", "trade")]
    assert events[-1]["payload"] == payload
    assert all(e["generation"] == 0 for e in events)
    assert stream.snapshot()["bybit"] is before
    assert stream.stats()["queued_updates"] == 0
    assert stream.next_event(0) is None
    with pytest.raises(RuntimeError):
        stream.next_update(0)
    with pytest.raises(RuntimeError):
        StreamBooks().next_event(0)


def test_event_disconnect_purges_only_affected_venue_and_resets_generation():
    stream = StreamBooks(event_mode=True)
    fill_stream(stream)
    stream._invalidate("bitget", "ConnectionClosed")
    first, invalidation = stream.next_event(0), stream.next_event(0)
    assert first["venue"] == "bybit" and first["kind"] == "book"
    assert invalidation["venue"] == "bitget" and invalidation["kind"] == "invalidate"
    assert invalidation["generation"] == 1 and invalidation["payload"] is None
    assert stream.stats()["purged_events"] == 1
    assert stream.stats()["bitget"]["invalidations"] == 1
    stream._invalidate("bitget")
    assert stream.next_event(0) is None
    stream._message("bitget", _BookParser("bitget"), json.dumps(bitget50()))
    assert stream.next_event(0)["generation"] == 1


def test_event_queue_overflow_is_fatal_and_counted():
    stream = StreamBooks(event_mode=True, max_updates=1)
    fill_stream(stream)
    assert stream.stats()["fatal_error"] == "event_queue_overflow"
    assert stream.stats()["dropped_events"] == 1
    assert stream.next_event(0) is None
    assert stream.snapshot() is None


def test_event_control_frames_not_published_and_failed_validation_not_published():
    stream = StreamBooks(event_mode=True)
    parser = _BookParser("bybit")
    stream._message("bybit", parser, json.dumps({"op": "subscribe", "success": True}))
    assert stream.next_event(0) is None
    with pytest.raises(MarketDataError):
        stream._message("bybit", parser, json.dumps(bybit("delta")))
    assert stream.next_event(0) is None


def test_event_timestamps_are_fifo_even_if_clock_goes_back(monkeypatch):
    from btc_cta.cross_arb import stream as module
    ticks = iter([2000000000, 1900000000, 1800000000])
    monkeypatch.setattr(module.time, "time_ns", lambda: next(ticks))
    stream = StreamBooks(event_mode=True)
    fill_stream(stream)
    stream._invalidate("bitget")
    events = [stream.next_event(0), stream.next_event(0)]
    assert [e["received_at_ms"] for e in events] == [2000, 2000]


def contract_payload(payload, symbol="HFTUSDT"):
    value = json.loads(json.dumps(payload))
    if "topic" in value:
        value["topic"] = value["topic"].replace("BTCUSDT", symbol)
    if "arg" in value:
        value["arg"]["symbol"] = symbol
    rows = value.get("data", [])
    if isinstance(rows, dict):
        rows = [rows]
    for row in rows:
        if "s" in row:
            row["s"] = symbol
    return value


@pytest.mark.parametrize("symbol", ["HFTUSDT", "1000PEPEUSDT", "WIFUSDT", "A" * 24 + "USDT"])
def test_contract_symbol_validation_and_dynamic_subscriptions(symbol):
    from btc_cta.cross_arb.stream import subscriptions_for, validate_symbol
    assert validate_symbol(symbol) == symbol
    result = subscriptions_for(symbol)
    assert result["bybit"]["args"] == [f"orderbook.50.{symbol}", f"publicTrade.{symbol}"]
    assert {row["symbol"] for row in result["bitget"]["args"]} == {symbol}
    assert {row["topic"] for row in result["bitget"]["args"]} == {"books50", "publicTrade"}
    assert SUBSCRIPTIONS["bybit"]["args"][0] == "orderbook.50.BTCUSDT"
    stream = StreamBooks(symbol=symbol)
    assert stream.symbol == symbol and stream.stats()["symbol"] == symbol


@pytest.mark.parametrize("symbol", [None, True, 123, "", "USDT", "AUSDT", "hftUSDT", "HFTUSD",
                                     "HFTUSDT ", "HFTUSDT\n", "HFT/USDT", "A" * 25 + "USDT"])
def test_invalid_contract_symbols_are_rejected_by_all_stream_entrypoints(symbol):
    from btc_cta.cross_arb.stream import subscriptions_for, validate_symbol
    for constructor in (validate_symbol, subscriptions_for,
                        lambda s: StreamBooks(symbol=s), lambda s: _BookParser("bybit", s)):
        with pytest.raises(MarketDataError, match="symbol"):
            constructor(symbol)


@pytest.mark.parametrize("venue", ["bybit", "bitget"])
def test_nonbtc_snapshot_delta_and_public_trade_are_strictly_bound_to_symbol(venue):
    parser = _BookParser(venue, "HFTUSDT")
    snapshot = bybit() if venue == "bybit" else bitget50()
    update = (bybit("delta", 12, 22, 1001, [["100", "0"], ["99", "4"]], [])
              if venue == "bybit" else
              bitget50("update", 12, 9, "1001", [["100", "0"], ["99", "4"]], []))
    first = parser.parse(contract_payload(snapshot), 1100)
    second = parser.parse(contract_payload(update), 1101)
    assert first.bids[0][0] == Decimal("100")
    assert second.bids == ((Decimal("99"), Decimal("4")),)
    assert parser.parse(contract_payload(trade_payload(venue)), 1102) is None
    assert parser.trade_ticks == 2


@pytest.mark.parametrize("venue", ["bybit", "bitget"])
def test_mixed_contract_trade_batch_rejected_before_trade_counter_changes(venue):
    parser = _BookParser(venue, "HFTUSDT")
    payload = contract_payload(trade_payload(venue))
    payload["data"][1]["s"] = "BTCUSDT"
    with pytest.raises(MarketDataError, match="symbol"):
        parser.parse(payload, 1100)
    assert parser.trade_ticks == 0
    payload["data"][1]["s"] = "HFTUSDT"
    parser.parse(payload, 1101)
    assert parser.trade_ticks == 2


@pytest.mark.parametrize("venue", ["bybit", "bitget"])
def test_wrong_contract_snapshot_does_not_poison_book_or_sequence(venue):
    parser = _BookParser(venue, "HFTUSDT")
    snapshot = bybit() if venue == "bybit" else bitget50()
    parser.parse(contract_payload(snapshot), 1100)
    initial = (parser.seq, parser.timestamp, parser.bids.copy(), parser.asks.copy())
    with pytest.raises(MarketDataError):
        parser.parse(snapshot, 1101)
    assert (parser.seq, parser.timestamp, parser.bids, parser.asks) == initial
    conflicting = contract_payload(snapshot)
    row = conflicting["data"] if venue == "bybit" else conflicting["data"][0]
    row["symbol"] = "BTCUSDT"
    with pytest.raises(MarketDataError, match="symbol"):
        parser.parse(conflicting, 1101)
    assert (parser.seq, parser.timestamp, parser.bids, parser.asks) == initial


@pytest.mark.parametrize("venue", ["bybit", "bitget"])
def test_nonbtc_receiver_uses_dynamic_subscription_and_bound_parser(monkeypatch, venue):
    from btc_cta.cross_arb import stream as module
    stream = StreamBooks(proxy=None, symbol="HFTUSDT", event_mode=True)
    sent = []

    class FakeConnection:
        calls = 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            stream._stop.set()

        def send(self, value):
            sent.append(json.loads(value))

        def recv(self, *, timeout):
            self.calls += 1
            if self.calls == 1:
                payload = bybit() if venue == "bybit" else bitget50()
                return json.dumps(contract_payload(payload))
            raise OSError("test connection ended")

    monkeypatch.setattr(module, "connect", lambda *args, **kwargs: FakeConnection())
    stream._run(venue)
    assert sent == [module.subscriptions_for("HFTUSDT")[venue]]
    assert stream.stats()[venue]["book_updates"] == 1


def test_stream_rejects_a_parser_bound_to_another_contract_before_publication():
    stream = StreamBooks(symbol="HFTUSDT", event_mode=True)
    with pytest.raises(MarketDataError, match="identity"):
        stream._message("bybit", _BookParser("bybit"), json.dumps(bybit()))
    assert stream.next_event(0) is None
    assert stream.stats()["bybit"]["book_updates"] == 0


def bitget_full(action="snapshot", seq=10, pseq=0, ts="1000", bids=None, asks=None):
    value = bitget50(action, seq, pseq, ts, bids, asks)
    value["arg"]["topic"] = "books"
    return value


@pytest.mark.parametrize("channel", ["books", "books50"])
def test_selectable_bitget_full_depth_subscriptions_and_stats(channel):
    from btc_cta.cross_arb.stream import subscriptions_for
    result = subscriptions_for("SKLUSDT", bitget_depth_channel=channel)
    assert result["bitget"]["args"] == [
        {"instType": "usdt-futures", "topic": channel, "symbol": "SKLUSDT"},
        {"instType": "usdt-futures", "topic": "publicTrade", "symbol": "SKLUSDT"},
    ]
    stats = StreamBooks(symbol="SKLUSDT", bitget_depth_channel=channel).stats()
    assert stats["bitget_depth_channel"] == channel
    assert stats["book_channels"] == {"bybit": "orderbook.50", "bitget": channel}


@pytest.mark.parametrize("channel", [None, True, 50, "books5", "books500", "BOOKS", "books\n"])
def test_live_bitget_depth_channel_is_explicit_and_validated(channel):
    from btc_cta.cross_arb.stream import subscriptions_for
    with pytest.raises(MarketDataError, match="channel"):
        subscriptions_for("SKLUSDT", channel)
    with pytest.raises(MarketDataError, match="channel"):
        StreamBooks(symbol="SKLUSDT", bitget_depth_channel=channel)


def test_full_depth_keeps_deeper_levels_and_promotes_them_after_best_levels_deleted():
    parser = _BookParser("bitget", "SKLUSDT")
    bids = [[str(2000 - i), "1"] for i in range(197)]
    asks = [[str(3000 + i), "2"] for i in range(430)]
    first = parser.parse(contract_payload(bitget_full(bids=bids, asks=asks), "SKLUSDT"), 1100)
    assert len(first.bids) == len(first.asks) == 50
    assert len(parser.bids) == 197 and len(parser.asks) == 430
    deleted_bids = [[str(2000 - i), "0"] for i in range(60)]
    deleted_asks = [[str(3000 + i), "0"] for i in range(75)]
    update = bitget_full("update", 12, 9, "1001", deleted_bids, deleted_asks)
    second = parser.parse(contract_payload(update, "SKLUSDT"), 1101)
    assert second.bids[0] == (Decimal("1940"), Decimal("1"))
    assert second.asks[0] == (Decimal("3075"), Decimal("2"))
    assert len(second.bids) == len(second.asks) == 50
    assert len(parser.bids) == 137 and len(parser.asks) == 355
    assert first.bids[0][0] == Decimal("2000")


def test_full_depth_sequence_gaps_leave_entire_book_unchanged():
    parser = _BookParser("bitget")
    parser.parse(bitget_full(), 1100)
    parser.parse(bitget_full("update", 12, 9, "1001", [["100", "5"]], []), 1101)
    before = (parser.seq, parser.timestamp, parser.bids.copy(), parser.asks.copy())
    with pytest.raises(MarketDataError, match="sequence gap"):
        parser.parse(bitget_full("update", 15, 13, "1002", [["100", "0"]], []), 1102)
    assert (parser.seq, parser.timestamp, parser.bids, parser.asks) == before


@pytest.mark.parametrize("maximum", [0, 1001, -1, True, None, "bad", "1.5"])
def test_full_depth_maximum_depth_must_be_bounded_integer_when_present(maximum):
    parser = _BookParser("bitget")
    value = bitget_full()
    value["data"][0]["maxDepth"] = maximum
    with pytest.raises(MarketDataError, match="maximum depth"):
        parser.parse(value, 1100)
    assert parser.seq is None and parser.bids == parser.asks == {}


@pytest.mark.parametrize("maximum", [1, 1000, "1000"])
def test_full_depth_valid_optional_maximum_depth(maximum):
    parser = _BookParser("bitget")
    value = bitget_full()
    value["data"][0]["maxDepth"] = maximum
    assert parser.parse(value, 1100) is not None


def test_full_depth_fails_closed_when_update_exceeds_1000_known_levels():
    parser = _BookParser("bitget")
    bids = [[str(2000 - i), "1"] for i in range(1000)]
    parser.parse(bitget_full(bids=bids, asks=[["3000", "2"]]), 1100)
    before = parser.bids.copy()
    with pytest.raises(MarketDataError, match="exceeds maximum"):
        parser.parse(bitget_full("update", 12, 9, "1001", [["500", "1"]], []), 1101)
    assert parser.seq == 10 and parser.bids == before
    # Deletion and insertion in the same frame can stay within the bound.
    result = parser.parse(bitget_full("update", 12, 9, "1001",
                                     [["2000", "0"], ["500", "1"]], []), 1101)
    assert result is not None and len(parser.bids) == 1000


def test_full_depth_declared_maximum_persists_across_updates_without_it():
    parser = _BookParser("bitget")
    snapshot = bitget_full()
    snapshot["data"][0]["maxDepth"] = 1
    parser.parse(snapshot, 1100)
    with pytest.raises(MarketDataError, match="exceeds maximum"):
        parser.parse(bitget_full("update", 12, 9, "1001", [["99", "1"]], []), 1101)
    assert parser.bids == {Decimal("100"): Decimal("2")}


def test_full_depth_batch_over_1000_or_crossed_or_empty_side_never_commits():
    parser = _BookParser("bitget")
    too_many = [[str(2000 - i), "1"] for i in range(1001)]
    with pytest.raises(MarketDataError, match="depth"):
        parser.parse(bitget_full(bids=too_many, asks=[["3000", "1"]]), 1100)
    parser.parse(bitget_full(), 1100)
    for bids in ([["102", "1"]], [["100", "0"]]):
        with pytest.raises(MarketDataError):
            parser.parse(bitget_full("update", 12, 9, "1001", bids, []), 1101)
        assert parser.seq == 10 and parser.bids == {Decimal("100"): Decimal("2")}


def test_full_depth_channel_cannot_start_with_an_update_after_other_channel_snapshot():
    parser = _BookParser("bitget")
    parser.parse(bitget50(), 1100)
    with pytest.raises(MarketDataError, match="channel changed"):
        parser.parse(bitget_full("update", 12, 9, "1001"), 1101)
    assert parser.seq == 10
    assert parser.parse(bitget_full("snapshot", 13, 0, "1002"), 1102) is not None


def test_live_receiver_sends_selected_full_depth_subscription(monkeypatch):
    from btc_cta.cross_arb import stream as module
    stream = StreamBooks(symbol="SKLUSDT", bitget_depth_channel="books", event_mode=True)
    sent = []

    class FakeConnection:
        calls = 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            stream._stop.set()

        def send(self, value):
            sent.append(json.loads(value))

        def recv(self, *, timeout):
            self.calls += 1
            if self.calls == 1:
                return json.dumps(contract_payload(bitget_full(), "SKLUSDT"))
            if self.calls == 2:
                update = bitget_full("update", 12, 9, "1001", [["100", "1"]], [])
                return json.dumps(contract_payload(update, "SKLUSDT"))
            raise OSError("test connection ended")

    monkeypatch.setattr(module, "connect", lambda *args, **kwargs: FakeConnection())
    stream._run("bitget")
    assert sent == [module.subscriptions_for("SKLUSDT", "books")["bitget"]]
    assert stream.stats()["bitget"]["book_updates"] == 2
