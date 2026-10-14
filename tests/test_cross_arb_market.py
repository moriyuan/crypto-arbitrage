from copy import deepcopy
from decimal import Decimal
from threading import Barrier

import httpx
import pytest

from btc_cta.cross_arb.market import (
    Book,
    InsufficientDepth,
    MarketData,
    MarketDataError,
    vwap,
)

D = Decimal
TS = 1_700_000_000_000


def book_payload(venue: str) -> dict:
    if venue == "bybit":
        return {
            "retCode": 0,
            "result": {
                "s": "BTCUSDT",
                "b": [["100", "0.2"], ["99", "0.3"]],
                "a": [["101", "0.1"], ["102", "0.4"]],
                "ts": TS + 10,
                "cts": TS,
            },
        }
    return {
        "code": "00000",
        "data": {
            "bids": [[100, 0.2], [99, 0.3]],
            "asks": [[101, 0.1], [102, 0.4]],
            "ts": str(TS),
            "precision": "scale0",
            "scale": "0.1",
        },
    }


def rules_payload(venue: str) -> dict:
    if venue == "bybit":
        return {
            "retCode": 0,
            "result": {
                "category": "linear",
                "list": [
                    {
                        "symbol": "BTCUSDT",
                        "baseCoin": "BTC",
                        "quoteCoin": "USDT",
                        "settleCoin": "USDT",
                        "contractType": "LinearPerpetual",
                        "status": "Trading",
                        "lotSizeFilter": {
                            "qtyStep": "0.001",
                            "minOrderQty": "0.001",
                            "minNotionalValue": "5",
                        },
                        "priceFilter": {"tickSize": "0.1"},
                    }
                ],
            },
        }
    return {
        "code": "00000",
        "data": [
            {
                "symbol": "BTCUSDT",
                "baseCoin": "BTC",
                "quoteCoin": "USDT",
                "supportMarginCoins": ["USDT"],
                "symbolType": "perpetual",
                "symbolStatus": "normal",
                "sizeMultiplier": "0.001",
                "minTradeNum": "0.001",
                "minTradeUSDT": "5",
                "priceEndStep": "5",
                "pricePlace": "2",
            }
        ],
    }


def test_fetches_concurrently_and_normalizes_exact_decimal_books(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    barrier = Barrier(2)
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        barrier.wait(timeout=2)
        venue = "bybit" if request.url.host == "api.bybit.com" else "bitget"
        return httpx.Response(200, json=book_payload(venue))

    with MarketData(transport=httpx.MockTransport(handler)) as market:
        books = market.fetch_books()
    assert set(books) == {"bybit", "bitget"}
    assert all(b.exchange_ts_ms == TS for b in books.values())
    assert all(b.received_at_ms >= TS and b.request_ms >= 0 for b in books.values())
    assert books["bitget"].bids[0] == (D("100"), D("0.2"))
    assert all(r.method == "GET" and r.url.scheme == "https" for r in requests)
    assert all("authorization" not in r.headers for r in requests)
    bybit = next(r for r in requests if r.url.host == "api.bybit.com")
    bitget = next(r for r in requests if r.url.host == "api.bitget.com")
    assert bybit.url.path == "/v5/market/orderbook"
    assert dict(bybit.url.params) == {"category": "linear", "symbol": "BTCUSDT", "limit": "50"}
    assert dict(bitget.url.params) == {
        "symbol": "BTCUSDT",
        "productType": "USDT-FUTURES",
        "precision": "scale0",
        "limit": "50",
    }


def test_rule_units_and_bitget_price_end_step() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        venue = "bybit" if request.url.host == "api.bybit.com" else "bitget"
        expected = (
            "/v5/market/instruments-info" if venue == "bybit" else "/api/v2/mix/market/contracts"
        )
        assert request.url.path == expected
        return httpx.Response(200, json=rules_payload(venue))

    with MarketData(transport=httpx.MockTransport(handler)) as market:
        rules = market.fetch_rules()
    assert rules["bybit"].qty_step == D("0.001")
    assert rules["bitget"].min_qty == D("0.001")
    assert rules["bitget"].min_notional == D("5")
    assert rules["bitget"].price_tick == D("0.05")


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-1", "0", True, "1e999999"])
def test_rejects_invalid_depth_numbers(bad: object) -> None:
    payload = book_payload("bybit")
    payload["result"]["a"][0][0] = bad

    def handler(request: httpx.Request) -> httpx.Response:
        data = payload if request.url.host == "api.bybit.com" else book_payload("bitget")
        return httpx.Response(200, json=data)

    with (
        MarketData(transport=httpx.MockTransport(handler)) as m,
        pytest.raises(MarketDataError),
    ):
        m.fetch_books()


@pytest.mark.parametrize(
    "change", ["crossed", "unsorted", "duplicate", "empty", "oversize", "symbol", "timestamp"]
)
def test_rejects_malformed_books(change: str) -> None:
    payload = book_payload("bybit")
    data = payload["result"]
    if change == "crossed":
        data["a"][0][0] = "100"
    elif change == "unsorted":
        data["b"].reverse()
    elif change == "duplicate":
        data["b"][1][0] = "100"
    elif change == "empty":
        data["a"] = []
    elif change == "oversize":
        data["b"] *= 501
    elif change == "symbol":
        data["s"] = "ETHUSDT"
    else:
        data["cts"] = "nan"

    def handler(request: httpx.Request) -> httpx.Response:
        data = payload if request.url.host == "api.bybit.com" else book_payload("bitget")
        return httpx.Response(200, json=data)

    with (
        MarketData(transport=httpx.MockTransport(handler)) as m,
        pytest.raises(MarketDataError),
    ):
        m.fetch_books()


@pytest.mark.parametrize(
    "venue,field,bad",
    [
        ("bybit", "settleCoin", "USDC"),
        ("bybit", "status", "PreLaunch"),
        ("bitget", "symbolType", "delivery"),
        ("bitget", "symbolStatus", "maintain"),
        ("bitget", "pricePlace", "999999"),
        ("bitget", "sizeMultiplier", "NaN"),
    ],
)
def test_rejects_wrong_or_invalid_contract_rules(venue: str, field: str, bad: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        current = "bybit" if request.url.host == "api.bybit.com" else "bitget"
        payload = rules_payload(current)
        if current == venue:
            rows = payload["result"]["list"] if current == "bybit" else payload["data"]
            rows[0][field] = bad
        return httpx.Response(200, json=payload)

    with (
        MarketData(transport=httpx.MockTransport(handler)) as market,
        pytest.raises(MarketDataError),
    ):
        market.fetch_rules()


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(302, headers={"Location": "http://127.0.0.1:1/private"}),
        httpx.Response(429, json={"message": "rate limited"}),
        httpx.Response(200, json={"retCode": 10006}),
        httpx.Response(200, content=b"not json"),
        httpx.Response(200, content=b"[]"),
        httpx.Response(200, content=b"x" * (1024 * 1024 + 1)),
    ],
)
def test_rejects_http_api_and_body_failures_without_following_redirects(
    response: httpx.Response,
) -> None:
    hosts = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        return deepcopy(response)

    with (
        MarketData(transport=httpx.MockTransport(handler)) as market,
        pytest.raises(MarketDataError),
    ):
        market.fetch_books()
    assert set(hosts) <= {"api.bybit.com", "api.bitget.com"}


def test_rejects_chunked_oversize_body_without_content_length() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=httpx.ByteStream(b"x" * (1024 * 1024 + 1)))

    with (
        MarketData(transport=httpx.MockTransport(handler)) as market,
        pytest.raises(MarketDataError, match="size limit"),
    ):
        market.fetch_books()


def test_rejects_aggregated_bitget_book() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        venue = "bybit" if request.url.host == "api.bybit.com" else "bitget"
        payload = book_payload(venue)
        if venue == "bitget":
            payload["data"]["precision"] = "scale1"
        return httpx.Response(200, json=payload)

    with (
        MarketData(transport=httpx.MockTransport(handler)) as market,
        pytest.raises(MarketDataError, match="aggregated"),
    ):
        market.fetch_books()


@pytest.mark.parametrize(
    "proxy",
    [
        "http://example.com:7897",
        "http://127.0.0.1:7897/path",
        "http://user:pass@127.0.0.1:7897",
        "http://127.0.0.1:7897?x=1",
        "https://127.0.0.1:7897",
        "http://localhost",
    ],
)
def test_proxy_is_limited_to_explicit_loopback_http(proxy: str) -> None:
    with pytest.raises(MarketDataError, match="proxy"):
        MarketData(proxy=proxy)


@pytest.mark.parametrize(
    "proxy", ["http://127.0.0.1:7897", "http://localhost:7897", "http://[::1]:7897"]
)
def test_accepts_loopback_proxy(proxy: str) -> None:
    with MarketData(proxy=proxy):
        pass


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_rejects_unbounded_timeout(timeout: float) -> None:
    with pytest.raises(MarketDataError):
        MarketData(timeout=timeout)


def test_vwap_walks_correct_side_and_refuses_partial_depth() -> None:
    book = Book(
        "bybit",
        ((D("100"), D("0.2")), (D("99"), D("0.3"))),
        ((D("101"), D("0.1")), (D("102"), D("0.4"))),
        TS,
        TS,
        1.0,
    )
    assert vwap(book, "buy", D("0.2")) == (D("101.5"), D("102"))
    assert vwap(book, "sell", D("0.4")) == (D("99.5"), D("99"))
    assert vwap(book, "buy", D("0.5")) == (D("101.8"), D("102"))
    with pytest.raises(InsufficientDepth):
        vwap(book, "buy", D("0.5001"))
    for qty in (D("0"), D("-1"), D("NaN"), D("Infinity")):
        with pytest.raises(MarketDataError):
            vwap(book, "buy", qty)
    with pytest.raises(MarketDataError):
        vwap(book, "hold", D("0.1"))


def test_skl_books_and_contract_rules_use_selected_base_units() -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.params["symbol"] == "SKLUSDT"
        venue = "bybit" if request.url.host == "api.bybit.com" else "bitget"
        is_rules = request.url.path.endswith(("instruments-info", "contracts"))
        payload = rules_payload(venue) if is_rules else book_payload(venue)
        if is_rules:
            row = payload["result"]["list"][0] if venue == "bybit" else payload["data"][0]
            row.update(symbol="SKLUSDT", baseCoin="SKL")
            if venue == "bybit":
                row["lotSizeFilter"].update(qtyStep="1", minOrderQty="1")
                row["priceFilter"]["tickSize"] = "0.000001"
            else:
                row.update(sizeMultiplier="1", minTradeNum="1", priceEndStep="1", pricePlace="6")
        elif venue == "bybit":
            payload["result"]["s"] = "SKLUSDT"
        else:
            payload["data"]["symbol"] = "SKLUSDT"
        return httpx.Response(200, json=payload)

    with MarketData(symbol="SKLUSDT", transport=httpx.MockTransport(handler)) as market:
        assert market.symbol == "SKLUSDT"
        books, rules = market.fetch_books(), market.fetch_rules()
    assert len(requests) == 4
    assert set(books) == {"bybit", "bitget"}
    assert all(rule.qty_step == D("1") for rule in rules.values())
    assert all(rule.price_tick == D("0.000001") for rule in rules.values())


@pytest.mark.parametrize("venue", ["bybit", "bitget"])
@pytest.mark.parametrize("kind", ["book", "rules"])
def test_selected_symbol_rejects_other_contract(venue: str, kind: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        current = "bybit" if request.url.host == "api.bybit.com" else "bitget"
        payload = rules_payload(current) if kind == "rules" else book_payload(current)
        expected = "BTCUSDT" if current == venue else "SKLUSDT"
        if kind == "rules":
            row = payload["result"]["list"][0] if current == "bybit" else payload["data"][0]
            row.update(symbol=expected, baseCoin=expected[:-4])
        elif current == "bybit":
            payload["result"]["s"] = expected
        else:
            payload["data"]["symbol"] = expected
        return httpx.Response(200, json=payload)

    with (
        MarketData(symbol="SKLUSDT", transport=httpx.MockTransport(handler)) as market,
        pytest.raises(MarketDataError),
    ):
        (market.fetch_rules if kind == "rules" else market.fetch_books)()


@pytest.mark.parametrize("symbol", ["sklusdt", "SKLUSDC", "SKL/USDT:USDT", "SKLUSDT?x=1", "", None])
def test_rest_rejects_invalid_symbol_before_request(symbol: object) -> None:
    with pytest.raises(MarketDataError, match="symbol"):
        MarketData(symbol=symbol)
