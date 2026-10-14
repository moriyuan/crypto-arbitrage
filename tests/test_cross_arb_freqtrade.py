from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest

from btc_cta.cross_arb.freqtrade import (
    FreqtradeClient,
    FreqtradeError,
    FreqtradeOutcomeUnknown,
    FreqtradeProtocolError,
    FreqtradeRequestError,
    TradeSnapshot,
)


def trade_payload(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "trade_id": 12,
        "pair": "BTC/USDT:USDT",
        "enter_tag": "ca1-cycle:bybit",
        "is_open": True,
        "is_short": False,
        "amount": 0.01,
        "open_rate": 60000,
        "has_open_orders": True,
        "funding_fees": -0.02,
        "orders": [
            {
                "order_id": "order-1",
                "ft_order_side": "buy",
                "is_open": True,
                "amount": 0.01,
                "filled": 0.004,
                "status": "open",
            }
        ],
    }
    payload.update(changes)
    return payload


def config_payload(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "dry_run": True,
        "exchange": "bybit",
        "trading_mode": "futures",
        "margin_mode": "isolated",
        "force_entry_enable": True,
        "short_allowed": True,
        "state": "running",
        "position_adjustment_enable": False,
        "stake_currency": "USDT",
    }
    payload.update(changes)
    return payload


def client(handler: object, **kwargs: object) -> FreqtradeClient:
    return FreqtradeClient(
        "http://127.0.0.1:8081",
        "local-user",
        "private-password",
        transport=httpx.MockTransport(handler),
        **kwargs,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:8081",
        "http://example.com:8081",
        "http://0.0.0.0:8081",
        "http://127.0.0.1.evil.invalid:8081",
        "http://user:secret@localhost:8081",
        "http://localhost:8081/other",
        "http://localhost:8081?secret=x",
        "http://localhost:8081#fragment",
        "http://169.254.169.254",
    ],
)
def test_refuses_non_loopback_or_ambiguous_endpoint(url: str) -> None:
    with pytest.raises(ValueError):
        FreqtradeClient(url, "user", "secret")


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8081",
        "http://127.0.0.1:8081/api/v1",
        "http://[::1]:8081/",
    ],
)
def test_normalizes_local_endpoint_and_uses_basic_auth(url: str) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=config_payload())

    with FreqtradeClient(url, "user", "secret", transport=httpx.MockTransport(handler)) as api:
        assert api.preflight("bybit")["dry_run"] is True
        assert "secret" not in repr(api)
    assert seen[0].url.path == "/api/v1/show_config"
    assert seen[0].headers["authorization"] == "Basic dXNlcjpzZWNyZXQ="
    assert seen[0].extensions["timeout"] == dict.fromkeys(
        ["connect", "read", "write", "pool"],
        10.0,
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"dry_run": False},
        {"dry_run": "true"},
        {"exchange": "bitget"},
        {"trading_mode": "spot"},
        {"margin_mode": "cross"},
        {"force_entry_enable": False},
        {"short_allowed": False},
        {"state": "stopped"},
        {"position_adjustment_enable": True},
        {"stake_currency": "BTC"},
    ],
)
def test_preflight_rejects_unsafe_runtime_config(changes: dict[str, object]) -> None:
    with (
        client(lambda _: httpx.Response(200, json=config_payload(**changes))) as api,
        pytest.raises(FreqtradeError),
    ):
        api.preflight("bybit")


def test_preflight_live_mode_requires_explicit_expectation() -> None:
    with client(lambda _: httpx.Response(200, json=config_payload(dry_run=False))) as api:
        assert api.preflight("bybit", expect_dry_run=False)["dry_run"] is False


def test_snapshot_measures_partial_fills_instead_of_requested_trade_amount() -> None:
    with client(lambda _: httpx.Response(200, json=[trade_payload()])) as api:
        snapshot = api.open_trades()[0]
    assert snapshot.amount == Decimal("0.01")
    assert snapshot.filled_amount == Decimal("0.004")
    assert snapshot.funding_fees == Decimal("-0.02")
    assert snapshot.entry_tag == "ca1-cycle:bybit"
    assert snapshot.orders[0].status == "open"
    assert snapshot.has_open_orders


@pytest.mark.parametrize(
    "is_short,entry_side,exit_side",
    [
        (False, "buy", "sell"),
        (True, "sell", "buy"),
    ],
)
def test_snapshot_subtracts_partial_exit_and_stoploss_fills(
    is_short: bool,
    entry_side: str,
    exit_side: str,
) -> None:
    orders = [
        {
            "order_id": "1",
            "ft_order_side": entry_side,
            "is_open": False,
            "amount": 0.01,
            "filled": 0.01,
            "status": "closed",
        },
        {
            "order_id": "2",
            "ft_order_side": exit_side,
            "is_open": False,
            "amount": 0.002,
            "filled": 0.002,
            "status": "closed",
        },
        {
            "order_id": "3",
            "ft_order_side": "stoploss",
            "is_open": True,
            "amount": 0.008,
            "filled": 0.001,
            "status": "open",
        },
    ]
    snapshot = TradeSnapshot.from_payload(trade_payload(is_short=is_short, orders=orders))
    assert snapshot.filled_amount == Decimal("0.007")
    assert snapshot.has_open_orders  # Open stoploss is still an outstanding exchange order.


def test_closed_trade_has_zero_exposure_even_if_historical_amount_remains() -> None:
    snapshot = TradeSnapshot.from_payload(trade_payload(is_open=False, has_open_orders=False))
    assert snapshot.filled_amount == 0


def test_null_fill_is_zero_not_requested_amount() -> None:
    orders = [
        {
            "order_id": "1",
            "ft_order_side": "buy",
            "is_open": True,
            "amount": 0.01,
            "filled": None,
            "status": "open",
        }
    ]
    assert TradeSnapshot.from_payload(trade_payload(orders=orders)).filled_amount == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"amount": "NaN"},
        {"is_open": "false"},
        {"trade_id": True},
        {"orders": []},
        {"orders": "invalid"},
        {"open_rate": -1},
    ],
)
def test_invalid_trade_payload_fails_closed(changes: dict[str, object]) -> None:
    with pytest.raises(FreqtradeProtocolError):
        TradeSnapshot.from_payload(trade_payload(**changes))


def test_force_enter_wire_contract_and_market_exit_cancel_routes() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("forceenter"):
            return httpx.Response(200, json=trade_payload())
        return httpx.Response(200, json={"result": "ok"})

    with client(handler) as api:
        result = api.force_enter(
            pair="BTC/USDT:USDT",
            side="long",
            stakeamount=Decimal("600"),
            leverage=Decimal("1"),
            price=Decimal("60000"),
            entry_tag="ca1-cycle:bybit",
        )
        api.force_exit(result.trade_id)
        api.cancel_open_orders(result.trade_id)
    assert json.loads(seen[0].content) == {
        "pair": "BTC/USDT:USDT",
        "side": "long",
        "stakeamount": 600.0,
        "leverage": 1.0,
        "price": 60000.0,
        "entry_tag": "ca1-cycle:bybit",
        "ordertype": "limit",
    }
    assert json.loads(seen[1].content) == {"tradeid": 12, "ordertype": "market"}
    assert [(r.method, r.url.path) for r in seen] == [
        ("POST", "/api/v1/forceenter"),
        ("POST", "/api/v1/forceexit"),
        ("DELETE", "/api/v1/trades/12/open-order"),
    ]


@pytest.mark.parametrize("failure", ["timeout", "http500", "invalidjson", "invalidtrade"])
def test_mutation_unknown_result_never_retries_or_leaks_error_content(failure: str) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("private-password in upstream message", request=request)
        if failure == "http500":
            return httpx.Response(500, text="private-password")
        if failure == "invalidjson":
            return httpx.Response(200, text="private-password")
        return httpx.Response(200, json={"status": "private-password"})

    with client(handler) as api, pytest.raises(FreqtradeOutcomeUnknown) as caught:
        api.force_enter(
            pair="BTC/USDT:USDT",
            side="long",
            stakeamount=Decimal(600),
            leverage=Decimal(1),
            price=Decimal(60000),
            entry_tag="ca1-cycle:bybit",
        )
    assert len(seen) == 1
    assert "private-password" not in str(caught.value)
    assert caught.value.__suppress_context__


def test_redirect_is_not_followed_and_body_is_not_exposed() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(302, headers={"location": "http://evil.invalid"}, text="secret")

    with client(handler) as api, pytest.raises(FreqtradeRequestError, match="302"):
        api.show_config()
    assert len(seen) == 1


def test_find_tag_searches_closed_history_and_paginates() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(str(request.url))
        if request.url.path.endswith("status"):
            return httpx.Response(200, json=[])
        offset = int(request.url.params["offset"])
        rows = (
            [trade_payload(trade_id=1, enter_tag="old", is_open=False)]
            if offset == 0
            else [trade_payload(is_open=False)]
        )
        return httpx.Response(
            200, json={"trades": rows, "trades_count": 1, "offset": offset, "total_trades": 2}
        )

    with client(handler, history_page_size=1) as api:
        snapshot = api.find_by_tag("ca1-cycle:bybit")
    assert snapshot is not None and snapshot.trade_id == 12 and not snapshot.is_open
    assert len(paths) == 3


def test_duplicate_tag_and_truncated_history_fail_closed() -> None:
    with (
        client(
            lambda _: httpx.Response(200, json=[trade_payload(), trade_payload(trade_id=13)])
        ) as api,
        pytest.raises(FreqtradeProtocolError, match="duplicate"),
    ):
        api.find_by_tag("ca1-cycle:bybit")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("status"):
            return httpx.Response(200, json=[])
        return httpx.Response(
            200,
            json={
                "trades": [trade_payload(enter_tag="other")],
                "trades_count": 1,
                "offset": 0,
                "total_trades": 2,
            },
        )

    with (
        client(handler, history_page_size=1, history_max_pages=1) as api,
        pytest.raises(FreqtradeProtocolError, match="history"),
    ):
        api.find_by_tag("ca1-cycle:bybit")


def test_cancel_unfilled_trade_can_return_unknown_then_trade_is_absent() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "DELETE":
            return httpx.Response(502, json={"error": "Trade not found"})
        if request.url.path.endswith("status"):
            return httpx.Response(200, json=[])
        if request.url.path.endswith("trades"):
            return httpx.Response(
                200, json={"trades": [], "trades_count": 0, "offset": 0, "total_trades": 0}
            )
        return httpx.Response(404, json={"detail": "Trade not found"})

    with client(handler) as api:
        with pytest.raises(FreqtradeOutcomeUnknown):
            api.cancel_open_orders(12)
        assert api.trade(12) is None
        assert api.find_by_tag("ca1-cycle:bybit") is None
    assert sum(request.method == "DELETE" for request in seen) == 1


def test_realized_profit_remains_unknown_if_worker_omits_it() -> None:
    assert TradeSnapshot.from_payload(trade_payload()).close_profit_abs is None
    snapshot = TradeSnapshot.from_payload(trade_payload(close_profit_abs=-0.143))
    assert snapshot.close_profit_abs == Decimal("-0.143")


@pytest.mark.parametrize(
    "changes", [{"pair": "ETH/USDT:USDT"}, {"is_short": True}, {"enter_tag": "other-cycle"}]
)
def test_mutation_response_must_match_requested_identity(changes: dict[str, object]) -> None:
    with (
        client(lambda _: httpx.Response(200, json=trade_payload(**changes))) as api,
        pytest.raises(FreqtradeOutcomeUnknown),
    ):
        api.force_enter(
            pair="BTC/USDT:USDT",
            side="long",
            stakeamount=Decimal(600),
            leverage=Decimal(1),
            price=Decimal(60000),
            entry_tag="ca1-cycle:bybit",
        )


@pytest.mark.parametrize("value", [Decimal(0), Decimal(-1), Decimal("NaN"), Decimal("Infinity")])
def test_invalid_order_numbers_are_rejected_before_transport(value: Decimal) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=trade_payload())

    with client(handler) as api, pytest.raises(ValueError):
        api.force_enter(
            pair="BTC/USDT:USDT",
            side="long",
            stakeamount=value,
            leverage=Decimal(1),
            price=Decimal(60000),
            entry_tag="ca1-cycle:bybit",
        )
    assert not seen


def test_boolean_fill_is_not_treated_as_numeric_zero() -> None:
    payload = trade_payload()
    payload["orders"][0]["filled"] = False  # type: ignore[index]
    with pytest.raises(FreqtradeProtocolError):
        TradeSnapshot.from_payload(payload)
