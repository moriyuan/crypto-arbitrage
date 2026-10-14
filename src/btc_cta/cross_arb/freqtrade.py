"""Narrow, fail-closed REST adapter for two local Freqtrade execution workers.

Wire contract verified against Freqtrade stable 2026.9 api_trading/api_schemas.
No mutation is retried: entry tags are reconciliation keys, not exchange-side
idempotency keys. Cancellation of a zero-fill initial entry can delete its trade;
Freqtrade may then return an error while serializing the now-absent trade. The
coordinator must reconcile even when the cancellation response is unknown.
"""

from __future__ import annotations

import ipaddress
import math
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, Self
from urllib.parse import urlsplit

import httpx


class FreqtradeError(RuntimeError):
    """Sanitized adapter error; never includes credentials or upstream bodies."""


class FreqtradeRequestError(FreqtradeError):
    """A read failed, or the server explicitly rejected a request."""


class FreqtradeProtocolError(FreqtradeError):
    """The response cannot safely establish the worker state."""


class FreqtradeOutcomeUnknown(FreqtradeError):
    """A mutation may have executed; reconcile before making any new decision."""


def _decimal(value: object, *, nonnegative: bool = True) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, str | float | int | Decimal):
        raise FreqtradeProtocolError("invalid numeric field") from None
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise FreqtradeProtocolError("invalid numeric field") from None
    if not result.is_finite() or (nonnegative and result < 0):
        raise FreqtradeProtocolError("invalid numeric field") from None
    return result


def _bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise FreqtradeProtocolError("invalid boolean field") from None
    return value


def _positive_id(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise FreqtradeProtocolError("invalid trade id") from None
    return value


def _text(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise FreqtradeProtocolError("invalid text field") from None
    return value


@dataclass(frozen=True, slots=True)
class OrderSnapshot:
    order_id: str
    side: str
    is_open: bool
    amount: Decimal
    filled: Decimal
    status: str = ""

    @classmethod
    def from_payload(cls, payload: object) -> Self:
        if not isinstance(payload, dict):
            raise FreqtradeProtocolError("invalid order response") from None
        side = _text(payload.get("ft_order_side"))
        if side not in {"buy", "sell", "stoploss"}:
            raise FreqtradeProtocolError("invalid order side") from None
        amount = _decimal(payload.get("amount"))
        raw_filled = payload.get("filled")
        filled = _decimal(0 if raw_filled is None else raw_filled)
        if filled > amount:
            raise FreqtradeProtocolError("order fill exceeds order amount") from None
        return cls(
            order_id=_text(payload.get("order_id")),
            side=side,
            is_open=_bool(payload.get("is_open")),
            amount=amount,
            filled=filled,
            status=_text(payload.get("status")),
        )


@dataclass(frozen=True, slots=True)
class TradeSnapshot:
    trade_id: int
    pair: str
    entry_tag: str | None
    is_open: bool
    is_short: bool
    amount: Decimal
    filled_amount: Decimal
    open_rate: Decimal
    has_open_orders: bool
    orders: tuple[OrderSnapshot, ...] = ()
    funding_fees: Decimal = Decimal(0)
    close_profit_abs: Decimal | None = None

    @classmethod
    def from_payload(cls, payload: object) -> Self:
        if not isinstance(payload, dict):
            raise FreqtradeProtocolError("invalid trade response") from None
        raw_orders = payload.get("orders")
        if not isinstance(raw_orders, list):
            raise FreqtradeProtocolError("missing order execution evidence") from None
        orders = tuple(OrderSnapshot.from_payload(order) for order in raw_orders)
        is_open = _bool(payload.get("is_open"))
        is_short = _bool(payload.get("is_short"))
        declared_open_orders = _bool(payload.get("has_open_orders"))
        if (is_open or declared_open_orders) and not orders:
            raise FreqtradeProtocolError("missing order execution evidence") from None
        if len({order.order_id for order in orders}) != len(orders):
            raise FreqtradeProtocolError("duplicate order execution evidence") from None
        entry_side = "sell" if is_short else "buy"
        entries = sum((order.filled for order in orders if order.side == entry_side), Decimal(0))
        exits = sum((order.filled for order in orders if order.side != entry_side), Decimal(0))
        if exits > entries:
            raise FreqtradeProtocolError("exit fills exceed entry fills") from None
        entry_tag = payload.get("enter_tag")
        if entry_tag is not None and not isinstance(entry_tag, str):
            raise FreqtradeProtocolError("invalid entry tag") from None
        profit = payload.get("close_profit_abs")
        return cls(
            trade_id=_positive_id(payload.get("trade_id")),
            pair=_text(payload.get("pair")),
            entry_tag=entry_tag,
            is_open=is_open,
            is_short=is_short,
            amount=_decimal(payload.get("amount")),
            filled_amount=entries - exits if is_open else Decimal(0),
            open_rate=_decimal(payload.get("open_rate")),
            has_open_orders=declared_open_orders or any(order.is_open for order in orders),
            orders=orders,
            funding_fees=_decimal(
                0 if payload.get("funding_fees") is None else payload["funding_fees"],
                nonnegative=False,
            ),
            close_profit_abs=_decimal(profit, nonnegative=False) if profit is not None else None,
        )


def _local_base_url(base_url: str) -> str:
    try:
        parsed = urlsplit(base_url)
        host = parsed.hostname
        is_loopback = host == "localhost"
        if not is_loopback and host:
            is_loopback = ipaddress.ip_address(host).is_loopback
        port = parsed.port
    except (ValueError, TypeError):
        raise ValueError("Freqtrade endpoint must be a loopback HTTP origin") from None
    if (
        parsed.scheme != "http"
        or not is_loopback
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/", "/api/v1", "/api/v1/"}
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise ValueError("Freqtrade endpoint must be a loopback HTTP origin") from None
    return f"http://{parsed.netloc}/api/v1/"


def _positive_number(value: object, name: str) -> float:
    try:
        numeric = float(_decimal(value))
    except (FreqtradeProtocolError, OverflowError, ValueError):
        raise ValueError(f"{name} must be a finite positive number") from None
    if not math.isfinite(numeric) or numeric <= 0:
        raise ValueError(f"{name} must be a finite positive number") from None
    return numeric


class FreqtradeClient:
    """Synchronous local worker client; credentials are held only by HTTP auth.

    The caller owns the trading authorization and persistent reconciliation state.
    ``preflight`` verifies the runtime flags; it is intentionally also usable for
    explicitly configured live workers, without changing any worker settings.
    """

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        timeout_seconds: float = 10.0,
        transport: httpx.BaseTransport | None = None,
        history_page_size: int = 500,
        history_max_pages: int = 20,
    ) -> None:
        endpoint = _local_base_url(base_url)
        if not username or not password:
            raise ValueError("Freqtrade REST credentials must be nonempty")
        if (
            isinstance(timeout_seconds, bool)
            or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 60
        ):
            raise ValueError("timeout must be between 0 and 60 seconds")
        if (
            type(history_page_size) is not int
            or type(history_max_pages) is not int
            or not 1 <= history_page_size <= 500
            or not 1 <= history_max_pages <= 100
        ):
            raise ValueError("invalid history pagination bounds")
        self._history_page_size = history_page_size
        self._history_max_pages = history_max_pages
        self._client = httpx.Client(
            base_url=endpoint,
            auth=httpx.BasicAuth(username, password),
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, object] | None = None,
        params: dict[str, str | int] | None = None,
        allow_missing: bool = False,
        require_json: bool = True,
    ) -> Any:
        mutation = method != "GET"
        error: type[FreqtradeError]
        try:
            response = self._client.request(method, path, json=body, params=params)
        except httpx.HTTPError:
            error = FreqtradeOutcomeUnknown if mutation else FreqtradeRequestError
            raise error("Freqtrade transport failed; reconcile mutation outcomes") from None
        if allow_missing and response.status_code == 404:
            return None
        if not response.is_success:
            error = (
                FreqtradeOutcomeUnknown
                if mutation and (response.status_code >= 500 or response.status_code == 408)
                else FreqtradeRequestError
            )
            raise error(f"Freqtrade request failed with HTTP {response.status_code}") from None
        if not require_json:
            return None
        try:
            return response.json()
        except (ValueError, UnicodeError):
            error = FreqtradeOutcomeUnknown if mutation else FreqtradeProtocolError
            raise error("invalid Freqtrade JSON response") from None

    def show_config(self) -> dict[str, Any]:
        payload = self._request("GET", "show_config")
        if not isinstance(payload, dict):
            raise FreqtradeProtocolError("invalid runtime configuration response") from None
        return payload

    def preflight(self, expected_exchange: str, *, expect_dry_run: bool = True) -> dict[str, Any]:
        if expected_exchange not in {"bybit", "bitget"} or not isinstance(expect_dry_run, bool):
            raise ValueError("invalid expected worker configuration")
        config = self.show_config()
        expected: dict[str, object] = {
            "dry_run": expect_dry_run,
            "exchange": expected_exchange,
            "trading_mode": "futures",
            "margin_mode": "isolated",
            "force_entry_enable": True,
            "short_allowed": True,
            "state": "running",
            "position_adjustment_enable": False,
            "stake_currency": "USDT",
        }
        for key, value in expected.items():
            actual = config.get(key)
            if type(actual) is not type(value) or actual != value:
                raise FreqtradeProtocolError(f"worker preflight failed: {key}") from None
        return config

    def open_trades(self) -> list[TradeSnapshot]:
        payload = self._request("GET", "status")
        if not isinstance(payload, list):
            raise FreqtradeProtocolError("invalid open trades response") from None
        return [TradeSnapshot.from_payload(trade) for trade in payload]

    def trade(self, trade_id: int) -> TradeSnapshot | None:
        trade_id = _positive_id(trade_id)
        payload = self._request("GET", f"trade/{trade_id}", allow_missing=True)
        return None if payload is None else TradeSnapshot.from_payload(payload)

    def find_by_tag(self, entry_tag: str) -> TradeSnapshot | None:
        _text(entry_tag)
        matches: dict[int, TradeSnapshot] = {}

        def collect(trades: list[TradeSnapshot]) -> None:
            for trade in trades:
                if trade.entry_tag == entry_tag:
                    matches[trade.trade_id] = trade
            if len(matches) > 1:
                raise FreqtradeProtocolError(
                    "duplicate entry tag requires reconciliation"
                ) from None

        collect(self.open_trades())
        offset = 0
        for _ in range(self._history_max_pages):
            payload = self._request(
                "GET",
                "trades",
                params={"limit": self._history_page_size, "offset": offset, "order_by_id": "true"},
            )
            if not isinstance(payload, dict) or not isinstance(payload.get("trades"), list):
                raise FreqtradeProtocolError("invalid trade history response") from None
            total = payload.get("total_trades")
            if not isinstance(total, int) or isinstance(total, bool) or total < 0:
                raise FreqtradeProtocolError("invalid trade history count") from None
            rows = payload["trades"]
            if payload.get("offset") != offset or payload.get("trades_count") != len(rows):
                raise FreqtradeProtocolError("inconsistent trade history pagination") from None
            collect([TradeSnapshot.from_payload(trade) for trade in rows])
            offset += len(rows)
            if offset >= total:
                return next(iter(matches.values()), None)
            if not rows:
                raise FreqtradeProtocolError("incomplete trade history response") from None
        raise FreqtradeProtocolError("trade history exceeds reconciliation limit") from None

    def force_enter(
        self,
        *,
        pair: str,
        side: Literal["long", "short"],
        stakeamount: Decimal,
        leverage: Decimal,
        price: Decimal,
        entry_tag: str,
    ) -> TradeSnapshot:
        if pair != "BTC/USDT:USDT" or side not in {"long", "short"}:
            raise ValueError("only BTC/USDT:USDT long/short perpetual entries are supported")
        if not isinstance(entry_tag, str) or not 1 <= len(entry_tag) <= 255:
            raise ValueError("entry_tag must contain 1 to 255 characters")
        body: dict[str, object] = {
            "pair": pair,
            "side": side,
            "ordertype": "limit",
            "entry_tag": entry_tag,
            "stakeamount": _positive_number(stakeamount, "stakeamount"),
            "leverage": _positive_number(leverage, "leverage"),
            "price": _positive_number(price, "price"),
        }
        payload = self._request("POST", "forceenter", body=body)
        try:
            snapshot = TradeSnapshot.from_payload(payload)
            if snapshot.pair != pair or snapshot.entry_tag != entry_tag:
                raise FreqtradeProtocolError("entry response identity mismatch")
            if snapshot.is_short != (side == "short"):
                raise FreqtradeProtocolError("entry response direction mismatch")
            return snapshot
        except FreqtradeProtocolError:
            raise FreqtradeOutcomeUnknown("entry response requires reconciliation") from None

    def force_exit(self, trade_id: int) -> None:
        payload = self._request(
            "POST",
            "forceexit",
            body={"tradeid": _positive_id(trade_id), "ordertype": "market"},
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("result"), str):
            raise FreqtradeOutcomeUnknown("exit response requires reconciliation") from None

    def cancel_open_orders(self, trade_id: int) -> None:
        self._request(
            "DELETE",
            f"trades/{_positive_id(trade_id)}/open-order",
            require_json=False,
        )
