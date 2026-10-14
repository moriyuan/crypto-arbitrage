"""Public USDT linear-perpetual REST snapshots; no account or order APIs.

Bybit: https://bybit-exchange.github.io/docs/v5/market/orderbook
Bitget: https://www.bitget.com/docs/catalog/classic-contract-market/classic-contract-market
Quantities are base-coin units, prices/notional are USDT.
REST snapshots are not atomic across venues.
"""

from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
from types import TracebackType
from typing import Any, TypeVar, cast
from urllib.parse import urlsplit

import httpx

_VENUES = ("bybit", "bitget")
_HOSTS = {"bybit": "https://api.bybit.com", "bitget": "https://api.bitget.com"}
_MAX_BODY = 1024 * 1024
_MAX_LEVELS = 1000
_MAX_NUMBER = Decimal("1e18")
_T = TypeVar("_T")
Levels = tuple[tuple[Decimal, Decimal], ...]


class MarketDataError(ValueError):
    """The public response cannot be safely used as an executable-price estimate."""


class InsufficientDepth(MarketDataError):
    """Displayed book depth cannot fill the entire requested quantity."""


def _decimal(value: object, label: str, *, allow_zero: bool = False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, str | int | Decimal):
        raise MarketDataError(f"invalid {label}")
    if len(str(value)) > 80:
        raise MarketDataError(f"invalid {label}")
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise MarketDataError(f"invalid {label}") from None
    if not result.is_finite():
        raise MarketDataError(f"nonfinite {label}")
    if result < 0 or (result == 0 and not allow_zero) or result > _MAX_NUMBER:
        raise MarketDataError(f"out-of-range {label}")
    # Bound precision as well as magnitude before any Decimal arithmetic.
    if cast(int, result.as_tuple().exponent) < -18:
        raise MarketDataError(f"excess precision in {label}")
    return result


def _integer(value: object, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, str | int):
        raise MarketDataError(f"invalid {label}")
    if len(str(value)) > 20 or not str(value).isdigit():
        raise MarketDataError(f"invalid {label}")
    result = int(value)
    if not minimum <= result <= maximum:
        raise MarketDataError(f"out-of-range {label}")
    return result


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MarketDataError(f"invalid {label}")
    return cast(dict[str, Any], value)


def _list(value: object, label: str) -> list[Any]:
    if not isinstance(value, list) or not 1 <= len(value) <= _MAX_LEVELS:
        raise MarketDataError(f"invalid {label} size")
    return value


def _levels(value: object, *, bids: bool) -> Levels:
    result = []
    for row in _list(value, "book"):
        if not isinstance(row, list) or len(row) != 2:
            raise MarketDataError("invalid book level")
        result.append((_decimal(row[0], "price"), _decimal(row[1], "quantity")))
    _validate_levels(tuple(result), bids=bids)
    return tuple(result)


def _validate_levels(levels: Levels, *, bids: bool) -> None:
    if not isinstance(levels, tuple) or not 1 <= len(levels) <= _MAX_LEVELS:
        raise MarketDataError("invalid book size")
    previous = None
    for level in levels:
        if not isinstance(level, tuple) or len(level) != 2:
            raise MarketDataError("invalid book level")
        price, quantity = level
        if not isinstance(price, Decimal) or not isinstance(quantity, Decimal):
            raise MarketDataError("book levels must use Decimal")
        _decimal(price, "price")
        _decimal(quantity, "quantity")
        if previous is not None and (price >= previous if bids else price <= previous):
            raise MarketDataError("book prices must be strictly sorted")
        previous = price


@dataclass(frozen=True)
class Book:
    venue: str
    bids: Levels
    asks: Levels
    received_at_ms: int
    exchange_ts_ms: int
    request_ms: float

    def __post_init__(self) -> None:
        if self.venue not in _VENUES:
            raise MarketDataError("unsupported venue")
        _validate_levels(self.bids, bids=True)
        _validate_levels(self.asks, bids=False)
        if self.bids[0][0] >= self.asks[0][0]:
            raise MarketDataError("crossed or locked book")
        _integer(self.received_at_ms, "receive timestamp", 1, 2**63 - 1)
        _integer(self.exchange_ts_ms, "exchange timestamp", 1, 2**63 - 1)
        if not math.isfinite(self.request_ms) or self.request_ms < 0:
            raise MarketDataError("invalid request duration")


@dataclass(frozen=True)
class Rules:
    qty_step: Decimal
    min_qty: Decimal
    min_notional: Decimal
    price_tick: Decimal

    def __post_init__(self) -> None:
        for field in ("qty_step", "min_qty", "min_notional", "price_tick"):
            value = getattr(self, field)
            if not isinstance(value, Decimal):
                raise MarketDataError("rules must use Decimal")
            _decimal(value, field, allow_zero=field == "min_notional")


def _proxy_url(proxy: str | None) -> str | None:
    if proxy is None:
        return None
    try:
        parts = urlsplit(proxy)
        port = parts.port
        valid = (
            parts.scheme == "http"
            and parts.hostname in {"127.0.0.1", "localhost", "::1"}
            and port is not None
            and 1 <= port <= 65535
            and parts.username is None
            and parts.password is None
            and not parts.path
            and not parts.query
            and not parts.fragment
        )
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise MarketDataError("proxy must be an explicit loopback HTTP host and port")
    host = "[::1]" if parts.hostname == "::1" else "127.0.0.1"
    return f"http://{host}:{port}"


def _reject_json_constant(value: str) -> None:
    raise MarketDataError("nonfinite JSON number")


class MarketData:
    """Synchronous, thread-safe HTTP client; each fetch starts two public GETs concurrently."""

    def __init__(
        self,
        proxy: str | None = None,
        timeout: float = 5.0,
        *,
        transport: httpx.BaseTransport | None = None,
        symbol: str = "BTCUSDT",
    ) -> None:
        if not isinstance(symbol, str) or re.fullmatch(r"[A-Z0-9]{2,24}USDT", symbol) is None:
            raise MarketDataError("invalid USDT perpetual symbol")
        self.symbol = symbol
        if not math.isfinite(timeout) or not 0 < timeout <= 60:
            raise MarketDataError("timeout must be finite and within (0, 60] seconds")
        proxy = _proxy_url(proxy)
        if proxy is not None and transport is not None:
            raise MarketDataError("proxy and injected transport cannot be combined")
        self._client = httpx.Client(
            proxy=proxy,
            timeout=timeout,
            transport=transport,
            verify=True,
            follow_redirects=False,
            trust_env=False,
            headers={"Accept": "application/json", "Accept-Encoding": "identity"},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> MarketData:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    @staticmethod
    def _parallel(fetch: Callable[[str], _T]) -> dict[str, _T]:
        # The context waits for both requests even if one fails: no orphan HTTP work.
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="public-book") as pool:
            futures = {venue: pool.submit(fetch, venue) for venue in _VENUES}
            return {venue: future.result() for venue, future in futures.items()}

    def _get(
        self,
        venue: str,
        path: str,
        params: dict[str, str],
    ) -> tuple[dict[str, Any], int, float]:
        started = time.perf_counter()
        try:
            with self._client.stream("GET", _HOSTS[venue] + path, params=params) as response:
                if response.status_code != 200:
                    raise MarketDataError(f"{venue} public HTTP status {response.status_code}")
                content_length = response.headers.get("content-length")
                if content_length is not None:
                    _integer(content_length, "response size", 0, _MAX_BODY)
                data = bytearray()
                for chunk in response.iter_bytes(chunk_size=65536):
                    if len(data) + len(chunk) > _MAX_BODY:
                        raise MarketDataError("public response exceeds size limit")
                    data.extend(chunk)
            received = int(time.time() * 1000)
            elapsed = (time.perf_counter() - started) * 1000
        except httpx.HTTPError:
            raise MarketDataError(f"{venue} public request failed") from None
        try:
            payload = _object(
                json.loads(
                    data,
                    parse_float=Decimal,
                    parse_constant=_reject_json_constant,
                ),
                "response",
            )
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise MarketDataError(f"{venue} returned invalid JSON") from None
        if venue == "bybit":
            if type(payload.get("retCode")) is not int or payload["retCode"] != 0:
                raise MarketDataError("bybit public API returned an error")
        elif payload.get("code") != "00000":
            raise MarketDataError("bitget public API returned an error")
        return payload, received, elapsed

    def fetch_books(self) -> dict[str, Book]:
        return self._parallel(self._fetch_book)

    def _fetch_book(self, venue: str) -> Book:
        if venue == "bybit":
            payload, received, elapsed = self._get(
                venue,
                "/v5/market/orderbook",
                {
                    "category": "linear",
                    "symbol": self.symbol,
                    "limit": "50",
                },
            )
            data = _object(payload.get("result"), "bybit book")
            if data.get("s") != self.symbol:
                raise MarketDataError("bybit returned another symbol")
            bids, asks, timestamp = data.get("b"), data.get("a"), data.get("cts")
        else:
            payload, received, elapsed = self._get(
                venue,
                "/api/v2/mix/market/merge-depth",
                {
                    "symbol": self.symbol,
                    "productType": "USDT-FUTURES",
                    "precision": "scale0",
                    "limit": "50",
                },
            )
            data = _object(payload.get("data"), "bitget book")
            # This endpoint normally omits symbol; the HTTPS request binds it.
            if any(key in data and data[key] != self.symbol for key in ("symbol", "s")):
                raise MarketDataError("bitget returned another symbol")
            if data.get("precision") != "scale0":
                raise MarketDataError("bitget returned aggregated book prices")
            bids, asks, timestamp = data.get("bids"), data.get("asks"), data.get("ts")
        return Book(
            venue,
            _levels(bids, bids=True),
            _levels(asks, bids=False),
            received,
            _integer(timestamp, "exchange timestamp", 1, 2**63 - 1),
            elapsed,
        )

    def fetch_rules(self) -> dict[str, Rules]:
        return self._parallel(self._fetch_rules)

    def _fetch_rules(self, venue: str) -> Rules:
        if venue == "bybit":
            payload, _, _ = self._get(
                venue,
                "/v5/market/instruments-info",
                {
                    "category": "linear",
                    "symbol": self.symbol,
                },
            )
            result = _object(payload.get("result"), "bybit instruments")
            if result.get("category") != "linear":
                raise MarketDataError("bybit returned another product type")
            rows = _list(result.get("list"), "instruments")
        else:
            payload, _, _ = self._get(
                venue,
                "/api/v2/mix/market/contracts",
                {
                    "symbol": self.symbol,
                    "productType": "USDT-FUTURES",
                },
            )
            rows = _list(payload.get("data"), "instruments")
        if len(rows) != 1:
            raise MarketDataError(f"expected exactly one {self.symbol} contract")
        item = _object(rows[0], "instrument")
        if any(
            item.get(k) != v
            for k, v in {
                "symbol": self.symbol,
                "baseCoin": self.symbol[:-4],
                "quoteCoin": "USDT",
            }.items()
        ):
            raise MarketDataError("unexpected contract identity")
        if venue == "bybit":
            if any(
                item.get(k) != v
                for k, v in {
                    "settleCoin": "USDT",
                    "contractType": "LinearPerpetual",
                    "status": "Trading",
                }.items()
            ):
                raise MarketDataError("bybit contract is not a trading USDT linear perpetual")
            lots = _object(item.get("lotSizeFilter"), "lot filter")
            prices = _object(item.get("priceFilter"), "price filter")
            return Rules(
                _decimal(lots.get("qtyStep"), "quantity step"),
                _decimal(lots.get("minOrderQty"), "minimum quantity"),
                _decimal(lots.get("minNotionalValue"), "minimum notional", allow_zero=True),
                _decimal(prices.get("tickSize"), "price tick"),
            )
        if (
            item.get("symbolType") != "perpetual"
            or item.get("symbolStatus") != "normal"
            or item.get("supportMarginCoins") != ["USDT"]
        ):
            raise MarketDataError("bitget contract is not a trading USDT linear perpetual")
        places = _integer(item.get("pricePlace"), "price places", 0, 18)
        end_step = _decimal(item.get("priceEndStep"), "price end step")
        return Rules(
            _decimal(item.get("sizeMultiplier"), "quantity step"),
            _decimal(item.get("minTradeNum"), "minimum quantity"),
            _decimal(item.get("minTradeUSDT"), "minimum notional", allow_zero=True),
            end_step * Decimal(10) ** -places,
        )


def vwap(book: Book, side: str, qty: Decimal) -> tuple[Decimal, Decimal]:
    """Walk the opposite side for the full base-coin quantity, returning VWAP and worst price."""
    if side not in {"buy", "sell"}:
        raise MarketDataError("side must be buy or sell")
    quantity = _decimal(qty, "requested quantity")
    levels = book.asks if side == "buy" else book.bids
    remaining = quantity
    notional = Decimal(0)
    with localcontext() as context:
        context.prec = 60
        for price, available in levels:
            take = min(remaining, available)
            notional += price * take
            remaining -= take
            if remaining == 0:
                return notional / quantity, price
    raise InsufficientDepth(f"{book.venue} has insufficient displayed {side} depth")
