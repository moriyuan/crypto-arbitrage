from __future__ import annotations

from copy import deepcopy
from decimal import Decimal

import pytest

from btc_cta.cross_arb.market import MarketDataError
from btc_cta.cross_arb.nowcast import FlowState


def book(venue="bybit", ts=1000, seq=1, bids=None, asks=None):
    bids = [["100", "2"], ["99", "3"]] if bids is None else bids
    asks = [["101", "2"], ["102", "3"]] if asks is None else asks
    if venue == "bybit":
        return {"topic": "orderbook.50.BTCUSDT", "type": "snapshot", "cts": ts,
                "data": {"s": "BTCUSDT", "seq": seq, "u": seq, "b": bids, "a": asks}}
    return {"arg": {"instType": "usdt-futures", "topic": "books5", "symbol": "BTCUSDT"},
            "action": "snapshot", "data": [{"ts": str(ts), "seq": seq, "b": bids, "a": asks}]}


def tick(trade_id="one", ts=1001, qty="1", price="101", side="Buy", **extra):
    return {"s": "BTCUSDT", "i": trade_id, "T": ts, "p": price, "v": qty, "S": side, **extra}


def trades(*items, venue="bybit"):
    if venue == "bybit":
        return {"topic": "publicTrade.BTCUSDT", "data": list(items)}
    return {"arg": {"instType": "usdt-futures", "topic": "publicTrade", "symbol": "BTCUSDT"},
            "data": list(items)}


def state():
    value = FlowState("bybit")
    assert value.consume(book(), 1010)
    return value


def test_initial_and_control_frames_need_confirmed_book():
    value = FlowState("bybit")
    assert value.features(1000) is None
    assert not value.consume({"op": "subscribe", "success": True}, 1000)
    assert value.features(1000) is None


def test_immutable_book_and_stable_feature_schema():
    value = state()
    result = value.features(1010)
    assert result["midpoint"] == 100.5
    assert result["bestbid"] == 100
    assert result["bestask"] == 101
    assert result["numeric"]["imbalance_top1"] == 0
    assert result["numeric"]["imbalance_top5"] == 0
    assert result["projected_book"] == result["confirmed_book"]
    assert result["numeric"]["book_age_ms"] == 10
    assert all(isinstance(x, float) for x in result["numeric"].values())


def test_aggressor_trade_depletes_only_its_exact_price_without_refreshing_clock():
    value = state()
    confirmed = value.features(1010)["confirmed_book"]
    value.consume(trades(tick(qty="3")), 1011)
    result = value.features(1011)
    assert result["confirmed_book"] is confirmed
    assert confirmed.asks[0][1] == Decimal("2")
    assert result["projected_book"].asks == ((Decimal("102"), Decimal("3")),)
    assert result["projected_book"].received_at_ms == 1010
    assert result["projected_book"].exchange_ts_ms == 1000
    assert result["book_received_ms"] == 1010
    assert result["received_ms"] == 1011
    assert result["numeric"]["unreflected_buy_qty"] == 3
    assert result["diagnostics"]["unmatched_depletion_qty"] == 1


def test_sell_depletes_bid_and_unseen_price_does_not_sweep():
    value = state()
    value.consume(trades(tick(price="100", side="Sell"),
                         tick("two", qty="100", price="105")), 1011)
    result = value.features(1011)
    assert result["projected_book"].bids[0][1] == Decimal("1")
    assert result["projected_book"].asks[0][1] == Decimal("2")
    assert result["diagnostics"]["unmatched_depletion_qty"] == 100


@pytest.mark.parametrize("ts", [999, 1000])
def test_book_source_watermark_is_strict_and_does_not_double_count(ts):
    value = state()
    value.consume(trades(tick(ts=ts)), 1011)
    result = value.features(1011)
    assert result["projected_book"] == result["confirmed_book"]
    assert result["numeric"]["unreflected_buy_qty"] == 0
    assert result["numeric"]["trade_count_100ms"] == 1


def test_new_book_rebases_pending_trades_and_equal_timestamp_is_covered():
    value = state()
    value.consume(trades(tick("first", 1001), tick("later", 1003, qty="0.5")), 1011)
    value.consume(book(ts=1001, seq=2, asks=[["101", "1"], ["102", "3"]]), 1012)
    result = value.features(1012)
    assert result["projected_book"].asks[0][1] == Decimal("0.5")
    assert result["numeric"]["unreflected_buy_qty"] == 0.5
    value.consume(book(ts=1003, seq=3, asks=[["101", "0.5"], ["102", "3"]]), 1013)
    assert value.features(1013)["projected_book"] == value.features(1013)["confirmed_book"]


def test_duplicate_ids_within_and_across_batches_never_deplete_twice():
    value = state()
    value.consume(trades(tick(), tick()), 1011)
    assert not value.consume(trades(tick()), 1012)
    result = value.features(1012)
    assert result["projected_book"].asks[0][1] == Decimal("1")
    assert result["numeric"]["trade_count_500ms"] == 1
    assert result["diagnostics"]["duplicate_trades"] == 2


def test_conflicting_duplicate_and_malformed_batch_are_atomic():
    value = state()
    value.consume(trades(tick()), 1011)
    before = value.features(1011)
    with pytest.raises(MarketDataError, match="conflicting"):
        value.consume(trades(tick(qty="2")), 1012)
    invalid = tick("invalid")
    invalid["v"] = "0"
    with pytest.raises(MarketDataError):
        value.consume(trades(tick("new"), invalid), 1012)
    assert value.features(1011) == before


@pytest.mark.parametrize("extra", [{"RPI": True}, {"BT": True}, {"RPI": "true"}])
def test_rpi_block_or_ambiguous_flags_are_excluded_from_depletion(extra):
    value = state()
    value.consume(trades(tick(**extra)), 1011)
    result = value.features(1011)
    assert result["projected_book"] == result["confirmed_book"]
    assert result["diagnostics"]["excluded_trades"] == 1
    assert result["numeric"]["trade_count_100ms"] == 1


@pytest.mark.parametrize("flag,excluded", [("yes", 1), ("no", 0), ("unknown", 1)])
def test_bitget_isrpi_string_flag(flag, excluded):
    value = FlowState("bitget")
    value.consume(book("bitget"), 1010)
    value.consume(trades(tick(side="buy", isRPI=flag), venue="bitget"), 1011)
    result = value.features(1011)
    assert result["diagnostics"]["excluded_trades"] == excluded
    assert result["projected_book"].asks[0][1] == Decimal(1 + excluded)


def test_entire_visible_side_depleted_prediction_unavailable():
    value = state()
    value.consume(trades(tick(qty="2"), tick("two", price="102", qty="3")), 1011)
    result = value.features(1011)
    assert result["projected_book"] is None
    assert result["projected_midpoint"] is None
    assert result["numeric"]["projection_available"] == 0
    assert result["confirmed_book"].asks


def test_reset_requires_fresh_snapshot_and_discards_features():
    value = state()
    value.consume(trades(tick()), 1011)
    value.reset()
    assert value.features(1012) is None
    delta = book(ts=1005, seq=2)
    delta["type"] = "delta"
    with pytest.raises(MarketDataError, match="before snapshot"):
        value.consume(delta, 1012)
    value.consume(book(ts=1013, seq=1), 1014)
    assert value.features(1014)["numeric"]["trade_count_500ms"] == 0


def test_recent_trade_windows_have_exact_deadlines():
    value = state()
    value.consume(trades(tick()), 1011)
    assert value.features(1100)["numeric"]["signed_trade_qty_100ms"] == 1
    assert value.features(1101)["numeric"]["signed_trade_qty_100ms"] == 0
    assert value.features(1500)["numeric"]["trade_count_500ms"] == 1
    assert value.features(1501)["numeric"]["trade_count_500ms"] == 0


def test_arrival_order_can_differ_from_source_order_without_changing_projection():
    left, right = state(), state()
    first, second = tick("first", 1004, qty="0.5"), tick("second", 1002, qty="0.25")
    left.consume(trades(first), 1011)
    left.consume(trades(second), 1012)
    right.consume(trades(second), 1011)
    right.consume(trades(first), 1012)
    a, b = left.features(1012), right.features(1012)
    assert a["projected_book"] == b["projected_book"]
    assert a["numeric"]["last_trade_offset_bps"] == b["numeric"]["last_trade_offset_bps"]


def test_causal_prefix_cannot_read_future_and_old_receipts_are_rejected():
    value = state()
    before = deepcopy(value.features(1010))
    with pytest.raises(MarketDataError, match="receipt"):
        value.consume(trades(tick()), 1009)
    assert value.features(1010) == before
    value.consume(trades(tick()), 1011)
    assert value.features(1010) is None
    assert before["projected_book"].asks[0][1] == Decimal("2")


def test_ofi_signed_depth_and_midvelocity_decay():
    value = state()
    value.consume(book(ts=1010, seq=2, bids=[["100", "3"], ["99", "3"]]), 1020)
    first = value.features(1020)["numeric"]
    later = value.features(1120)["numeric"]
    assert first["depth_ofi_100ms"] == 1
    assert later["depth_ofi_100ms"] == pytest.approx(0.36787944117)
    value.consume(book(ts=1020, seq=3, bids=[["101", "3"], ["100", "3"]],
                       asks=[["102", "2"], ["103", "3"]]), 1030)
    assert value.features(1030)["numeric"]["mid_velocity_100ms_bps_s"] > 0


def test_finite_buffers_fail_closed_when_pending_trade_history_is_lost():
    value = FlowState("bybit", max_trades=2, max_seen=3)
    value.consume(book(), 1010)
    value.consume(trades(tick("one", qty="0.1"), tick("two", qty="0.1"),
                         tick("three", qty="0.1")), 1011)
    result = value.features(1011)
    assert result["diagnostics"]["buffered_trades"] == 2
    assert result["projected_book"] is None
    assert result["diagnostics"]["pending_history_lost"]
    value.consume(book(ts=1001, seq=2), 1012)
    assert value.features(1012)["projected_book"] is not None


def test_retired_id_time_watermark_prevents_duplicates_after_bounded_seen_eviction():
    value = FlowState("bybit", max_trades=2, max_seen=2)
    value.consume(book(), 1010)
    for n in range(1, 4):
        value.consume(trades(tick(str(n), 1000 + n, qty="0.1")), 1010 + n)
    assert not value.consume(trades(tick("1", 1001, qty="0.1")), 1014)
    assert value.features(1014)["diagnostics"]["retired_trades"] == 1


def test_topfive_features_do_not_depend_on_added_deep_levels():
    bids = [[str(100 - n), "1"] for n in range(7)]
    asks = [[str(101 + n), "1"] for n in range(7)]
    value = FlowState("bybit")
    value.consume(book(bids=bids, asks=asks), 1010)
    bids[-1][1] = "100"
    value.consume(book(ts=1001, seq=2, bids=bids, asks=asks), 1011)
    result = value.features(1011)["numeric"]
    assert result["imbalance_top5"] == 0
    assert result["depth_ofi_100ms"] == 0


def test_delayed_source_events_and_startup_history_do_not_become_fresh_flow():
    value = state()
    value.consume(trades(tick(ts=1001)), 2000)
    result = value.features(2000)
    assert result["numeric"]["trade_count_100ms"] == 0
    assert result["numeric"]["signed_trade_qty_500ms"] == 0
    assert result["numeric"]["last_trade_offset_bps"] == 0
    # It is still source-newer than the stale book, so the labelled projection
    # can apply it; outer source-age guards decide whether that book is usable.
    assert result["projected_book"].asks[0][1] == Decimal("1")


def test_future_source_clock_does_not_deplete_or_enter_window_before_its_time():
    value = state()
    value.consume(trades(tick(ts=1100)), 1011)
    early = value.features(1099)
    assert early["numeric"]["trade_count_500ms"] == 0
    assert early["projected_book"] == early["confirmed_book"]
    at_time = value.features(1100)
    assert at_time["numeric"]["trade_count_500ms"] == 1
    assert at_time["projected_book"].asks[0][1] == Decimal("1")


def test_distinct_future_suffixes_leave_identical_prefix_outputs():
    left, right = state(), state()
    first = trades(tick())
    left.consume(first, 1011)
    right.consume(first, 1011)
    prefix_left = left.features(1011)
    prefix_right = right.features(1011)
    left.consume(book(ts=1003, seq=2, bids=[["99", "4"]]), 1012)
    right.consume(trades(tick("other", 1002, side="Sell", price="100")), 1012)
    assert prefix_left == prefix_right
    assert left.features(1012) != right.features(1012)


def symbol_payload(payload, symbol="HFTUSDT"):
    result = deepcopy(payload)
    if "topic" in result:
        result["topic"] = result["topic"].replace("BTCUSDT", symbol)
    if "arg" in result:
        result["arg"]["symbol"] = symbol
    rows = result.get("data", [])
    if isinstance(rows, dict):
        rows = [rows]
    for row in rows:
        if "s" in row:
            row["s"] = symbol
    return result


@pytest.mark.parametrize("venue", ["bybit", "bitget"])
def test_nonbtc_flow_features_projection_and_reset_preserve_contract(venue):
    value = FlowState(venue, symbol="HFTUSDT")
    value.consume(symbol_payload(book(venue)), 1010)
    payload = trades(tick(side="Buy" if venue == "bybit" else "buy"), venue=venue)
    value.consume(symbol_payload(payload), 1011)
    result = value.features(1011)
    assert result["symbol"] == "HFTUSDT"
    assert result["projected_book"].asks[0][1] == Decimal("1")
    assert result["numeric"]["signed_trade_qty_100ms"] == 1
    value.reset()
    assert value.symbol == "HFTUSDT" and value.features(1012) is None
    with pytest.raises(MarketDataError):
        value.consume(book(venue), 1013)
    value.consume(symbol_payload(book(venue)), 1014)
    assert value.features(1014)["numeric"]["trade_count_500ms"] == 0


@pytest.mark.parametrize("venue", ["bybit", "bitget"])
def test_wrong_contract_book_or_mixed_trade_batch_cannot_modify_flow(venue):
    value = FlowState(venue, symbol="HFTUSDT")
    value.consume(symbol_payload(book(venue)), 1010)
    before = value.features(1010)
    payload = symbol_payload(trades(tick(side="Buy" if venue == "bybit" else "buy"),
                                    tick("other", side="Buy" if venue == "bybit" else "buy"),
                                    venue=venue))
    payload["data"][1]["s"] = "BTCUSDT"
    for invalid in (book(venue), payload):
        with pytest.raises(MarketDataError):
            value.consume(invalid, 1011)
        assert value.features(1010) == before


@pytest.mark.parametrize("symbol", ["USDT", "btcUSDT", "HFTUSD", "HFT/USDT", None])
def test_flow_rejects_invalid_symbols(symbol):
    with pytest.raises(MarketDataError, match="symbol"):
        FlowState("bybit", symbol=symbol)
