from __future__ import annotations

import copy
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from btc_cta.cross_arb.funding import FundingGuard
from btc_cta.cross_arb.market import MarketDataError

NOW = 1_791_446_400_000
NEXT = NOW + 8 * 60 * 60 * 1000


def payloads(symbol="BTCUSDT"):
    return {
        "/v5/market/tickers": {
            "retCode": 0,
            "time": NOW,
            "result": {
                "category": "linear",
                "list": [
                    {
                        "symbol": symbol,
                        "nextFundingTime": str(NEXT),
                        "fundingRate": "0.0001",
                        "markPrice": "62000",
                    }
                ],
            },
        },
        "/api/v2/mix/market/funding-time": {
            "code": "00000",
            "requestTime": NOW,
            "data": [{"symbol": symbol, "nextFundingTime": str(NEXT)}],
        },
        "/api/v2/mix/market/current-fund-rate": {
            "code": "00000",
            "requestTime": NOW,
            "data": [{"symbol": symbol, "fundingRate": "-0.0002", "nextUpdate": str(NEXT)}],
        },
        "/api/v2/mix/market/symbol-price": {
            "code": "00000",
            "requestTime": NOW,
            "data": [{"symbol": symbol, "markPrice": "62010", "ts": str(NOW)}],
        },
    }


@pytest.fixture
def make_guard(monkeypatch):
    guards = []
    monkeypatch.setattr("btc_cta.cross_arb.market.time.time", lambda: NOW / 1000)

    def create(data=None, symbol="BTCUSDT"):
        responses = copy.deepcopy(payloads(symbol) if data is None else data)

        def handler(request):
            assert request.method == "GET"
            assert request.url.scheme == "https"
            assert request.url.host in {"api.bybit.com", "api.bitget.com"}
            assert request.url.params["symbol"] == symbol
            assert "Authorization" not in request.headers
            value = responses[request.url.path]
            if isinstance(value, Exception):
                raise value
            return httpx.Response(200, json=value)

        guard = FundingGuard(proxy=None, symbol=symbol, transport=httpx.MockTransport(handler))
        guards.append(guard)
        return guard, responses

    yield create
    for guard in guards:
        guard.close()


def test_public_snapshot_is_serializable_and_keeps_negative_rate(make_guard):
    guard, _ = make_guard()
    result = guard.refresh(NOW)
    assert set(result) == {"bybit", "bitget"}
    assert result["bitget"]["rate"] == "-0.0002"
    assert result["bybit"]["mark"] == "62000"
    assert result["bitget"]["next_ms"] == NEXT
    assert json.loads(json.dumps(result)) == result
    assert guard.decision(NOW, 60) is None
    result["bybit"]["next_ms"] = 1
    assert guard.decision(NOW, 60) is None


def test_missing_schedule_blocks(make_guard):
    guard, _ = make_guard()
    assert guard.decision(NOW, 60) == "funding_schedule_missing:bybit"


def test_unknown_source_rejected(make_guard):
    guard, _ = make_guard()
    with pytest.raises(MarketDataError, match="unsupported funding source"):
        guard._fetch("other", NOW)


@pytest.mark.parametrize("age,blocked", [(75_000, False), (75_001, True), (-1, True)])
def test_stale_or_future_fetch_blocks(make_guard, age, blocked):
    guard, _ = make_guard()
    guard.refresh(NOW)
    assert (guard.decision(NOW + age, 60) is not None) is blocked


@pytest.mark.parametrize("path", list(payloads()))
def test_wrong_contract_identity_blocks(make_guard, path):
    data = payloads()
    item = data[path]["result"]["list"][0] if path.startswith("/v5") else data[path]["data"][0]
    item["symbol"] = "ETHUSDT"
    guard, _ = make_guard(data)
    guard.refresh(NOW)
    assert guard.decision(NOW, 60) is not None


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "1.1", True, "1e-10000"])
def test_invalid_rate_blocks(make_guard, value):
    data = payloads()
    data["/v5/market/tickers"]["result"]["list"][0]["fundingRate"] = value
    guard, _ = make_guard(data)
    guard.refresh(NOW)
    assert guard.decision(NOW, 60) == "funding_schedule_missing:bybit"


@pytest.mark.parametrize("value", ["NaN", "0", "-1", "Infinity"])
def test_invalid_mark_blocks(make_guard, value):
    data = payloads()
    data["/api/v2/mix/market/symbol-price"]["data"][0]["markPrice"] = value
    guard, _ = make_guard(data)
    guard.refresh(NOW)
    assert guard.decision(NOW, 60) == "funding_schedule_missing:bitget"


def test_stale_source_blocks(make_guard):
    data = payloads()
    data["/v5/market/tickers"]["time"] = NOW - 60_001
    guard, _ = make_guard(data)
    guard.refresh(NOW)
    assert guard.decision(NOW, 60) == "funding_schedule_missing:bybit"


@pytest.mark.parametrize(
    "remaining,holding,expected",
    [
        (0, 0, "funding_schedule_passed:bybit"),
        (-1, 0, "funding_schedule_passed:bybit"),
        (60_000, 0, "funding_settlement_approaching:bybit"),
        (120_000, 60, "funding_settlement_approaching:bybit"),
        (120_001, 60, None),
    ],
)
def test_settlement_boundaries(make_guard, remaining, holding, expected):
    data = payloads()
    data["/v5/market/tickers"]["result"]["list"][0]["nextFundingTime"] = str(NOW + remaining)
    guard, _ = make_guard(data)
    guard.refresh(NOW)
    assert guard.decision(NOW, holding) == expected


def test_inconsistent_bitget_schedule_blocks(make_guard):
    data = payloads()
    data["/api/v2/mix/market/current-fund-rate"]["data"][0]["nextUpdate"] = str(NEXT + 1)
    guard, _ = make_guard(data)
    guard.refresh(NOW)
    assert guard.decision(NOW, 60) == "funding_schedule_missing:bitget"


def test_failed_refresh_keeps_old_schedule_but_blocks(make_guard):
    guard, responses = make_guard()
    guard.refresh(NOW)
    responses["/v5/market/tickers"] = httpx.ConnectError("not exposed")
    result = guard.refresh(NOW + 30_000)
    assert result["bybit"]["next_ms"] == NEXT
    assert result["bybit"]["error"] == "funding_refresh_failed"
    assert guard.decision(NOW + 30_000, 60) == "funding_schedule_unknown:bybit"
    assert "not exposed" not in json.dumps(result)


def test_wrong_bybit_category_blocks(make_guard):
    data = payloads()
    data["/v5/market/tickers"]["result"]["category"] = "inverse"
    guard, _ = make_guard(data)
    guard.refresh(NOW)
    assert guard.decision(NOW, 60) == "funding_schedule_missing:bybit"


@pytest.mark.parametrize("horizon", [-1, float("nan"), float("inf"), True])
def test_bad_horizon_rejected(make_guard, horizon):
    guard, _ = make_guard()
    with pytest.raises(ValueError, match="holding horizon"):
        guard.decision(NOW, horizon)


@pytest.mark.parametrize("offset", [-60_001, 5001])
def test_each_bitget_source_timestamp_checked(make_guard, offset):
    data = payloads()
    data["/api/v2/mix/market/symbol-price"]["data"][0]["ts"] = str(NOW + offset)
    guard, _ = make_guard(data)
    guard.refresh(NOW)
    assert guard.decision(NOW, 60) == "funding_schedule_missing:bitget"


def test_refresh_publishes_both_schedules_together(make_guard, monkeypatch):
    guard, _ = make_guard()
    original = guard.refresh(NOW)
    ready, release = threading.Event(), threading.Event()

    def fetch(venue, now_ms):
        result = dict(original[venue])
        result["next_ms"] += 1000
        if venue == "bitget":
            ready.set()
            assert release.wait(5)
        return result

    monkeypatch.setattr(guard, "_fetch", fetch)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(guard.refresh, NOW + 1)
        try:
            assert ready.wait(5)
            during_refresh = guard.snapshot()
        finally:
            release.set()
        after_refresh = future.result(timeout=5)
    assert during_refresh == original
    assert all(after_refresh[v]["next_ms"] == NEXT + 1000 for v in original)


def test_skl_funding_uses_selected_contract_and_per_venue_times(make_guard):
    data = payloads("SKLUSDT")
    bitget_next = NOW + 4 * 60 * 60 * 1000
    data["/api/v2/mix/market/funding-time"]["data"][0]["nextFundingTime"] = str(bitget_next)
    data["/api/v2/mix/market/current-fund-rate"]["data"][0]["nextUpdate"] = str(bitget_next)
    guard, _ = make_guard(data, symbol="SKLUSDT")
    result = guard.refresh(NOW)
    assert all(row["symbol"] == "SKLUSDT" for row in result.values())
    assert result["bybit"]["next_ms"] == NEXT
    assert result["bitget"]["next_ms"] == bitget_next
    assert guard.decision(NOW, 60) is None


@pytest.mark.parametrize("path", list(payloads()))
def test_skl_funding_rejects_btc_response_on_every_endpoint(make_guard, path):
    data = payloads("SKLUSDT")
    item = data[path]["result"]["list"][0] if path.startswith("/v5") else data[path]["data"][0]
    item["symbol"] = "BTCUSDT"
    guard, _ = make_guard(data, symbol="SKLUSDT")
    guard.refresh(NOW)
    assert guard.decision(NOW, 60) is not None
