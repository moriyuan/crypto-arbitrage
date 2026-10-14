"""Public funding-settlement avoidance for USDT perpetual paper trading.

Current rates are indicative, not evidence of a settled funding payment.
Sources: https://bybit-exchange.github.io/docs/v5/market/tickers
https://www.bitget.com/docs/catalog/classic-contract-market/classic-contract-market
"""

from __future__ import annotations

import copy
import math
import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, InvalidOperation
from types import TracebackType
from typing import Any

import httpx

from .market import MarketData, MarketDataError, _decimal, _integer, _object

_VENUES = ("bybit", "bitget")
_MAX_AGE_MS = 75_000
_MAX_SOURCE_AGE_MS = 60_000
_BUFFER_MS = 60_000
_MAX_INTERVAL_MS = 24 * 60 * 60 * 1000


def _rate(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, str | int | Decimal):
        raise MarketDataError("invalid funding rate")
    if len(str(value)) > 80:
        raise MarketDataError("invalid funding rate")
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise MarketDataError("invalid funding rate") from None
    if not result.is_finite() or abs(result) > 1:
        raise MarketDataError("invalid funding rate")
    if int(result.as_tuple().exponent) < -18:
        raise MarketDataError("excess precision in funding rate")
    return str(result)


def _one(rows: object, symbol: str) -> dict[str, Any]:
    if not isinstance(rows, list) or len(rows) != 1:
        raise MarketDataError(f"expected one {symbol} funding record")
    row = _object(rows[0], "funding record")
    if row.get("symbol") != symbol:
        raise MarketDataError("unexpected funding contract identity")
    return row


def _timestamp(value: object) -> int:
    return _integer(value, "funding timestamp", 1, 2**63 - 1)


class FundingGuard:
    """Fail closed around unknown/stale schedules and settlement boundaries.

    The caller refreshes at most once per 30 seconds. A failed refresh keeps the
    last known timestamp for exposure auditing, but blocks new entries. This is
    only a schedule guard: it does not calculate or assert actual funding PnL.
    """

    def __init__(
        self,
        proxy: str | None = "http://127.0.0.1:7897",
        *,
        timeout: float = 5.0,
        transport: httpx.BaseTransport | None = None,
        symbol: str = "BTCUSDT",
    ) -> None:
        self._market = MarketData(proxy=proxy, timeout=timeout, transport=transport, symbol=symbol)
        self.symbol = self._market.symbol
        self._schedules: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def close(self) -> None:
        self._market.close()

    def __enter__(self) -> FundingGuard:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def snapshot(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self._schedules)

    def refresh(self, now_ms: int) -> dict[str, dict[str, Any]]:
        _timestamp(now_ms)
        updated = self.snapshot()
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="public-funding") as pool:
            futures = {venue: pool.submit(self._fetch, venue, now_ms) for venue in _VENUES}
            for venue, future in futures.items():
                try:
                    updated[venue] = future.result()
                except (MarketDataError, ValueError):
                    previous = dict(updated.get(venue, {}))
                    previous.update(error="funding_refresh_failed", attempted_ms=now_ms)
                    updated[venue] = previous
        with self._lock:
            self._schedules = updated
        return self.snapshot()

    def _fetch(self, venue: str, now_ms: int) -> dict[str, Any]:
        params = {"symbol": self.symbol, "productType": "USDT-FUTURES"}
        if venue == "bybit":
            payload, received, _ = self._market._get(
                venue, "/v5/market/tickers", {"category": "linear", "symbol": self.symbol}
            )
            result = _object(payload.get("result"), "bybit ticker")
            if result.get("category") != "linear":
                raise MarketDataError("unexpected funding product category")
            item = _one(result.get("list"), self.symbol)
            next_ms = _timestamp(item.get("nextFundingTime"))
            rate = _rate(item.get("fundingRate"))
            mark = str(_decimal(item.get("markPrice"), "mark price"))
            source_ms = _timestamp(payload.get("time"))
            source_times = [(source_ms, received)]
        elif venue == "bitget":
            schedule, schedule_received, _ = self._market._get(
                venue, "/api/v2/mix/market/funding-time", params
            )
            rates, rates_received, _ = self._market._get(
                venue, "/api/v2/mix/market/current-fund-rate", params
            )
            prices, prices_received, _ = self._market._get(
                venue, "/api/v2/mix/market/symbol-price", params
            )
            item = _one(schedule.get("data"), self.symbol)
            rate_item = _one(rates.get("data"), self.symbol)
            price_item = _one(prices.get("data"), self.symbol)
            next_ms = _timestamp(item.get("nextFundingTime"))
            if next_ms != _timestamp(rate_item.get("nextUpdate")):
                raise MarketDataError("inconsistent funding schedule")
            rate = _rate(rate_item.get("fundingRate"))
            mark = str(_decimal(price_item.get("markPrice"), "mark price"))
            received = min(schedule_received, rates_received, prices_received)
            source_times = [
                (_timestamp(schedule.get("requestTime")), schedule_received),
                (_timestamp(rates.get("requestTime")), rates_received),
                (_timestamp(prices.get("requestTime")), prices_received),
                (_timestamp(price_item.get("ts")), prices_received),
            ]
            source_ms = min(timestamp for timestamp, _ in source_times)
        else:
            raise MarketDataError("unsupported funding source")
        if any(
            not -5000 <= receipt - timestamp <= _MAX_SOURCE_AGE_MS
            for timestamp, receipt in source_times
        ):
            raise MarketDataError("stale funding source timestamp")
        if next_ms - now_ms > _MAX_INTERVAL_MS:
            raise MarketDataError("implausible funding settlement timestamp")
        return {
            "symbol": self.symbol,
            "next_ms": next_ms,
            "rate": rate,
            "mark": mark,
            "fetched_ms": received,
            "source_ms": source_ms,
            "error": None,
        }

    def decision(self, now_ms: int, max_holding_s: float) -> str | None:
        _timestamp(now_ms)
        if (
            isinstance(max_holding_s, bool)
            or not math.isfinite(max_holding_s)
            or not 0 <= max_holding_s <= 86400
        ):
            raise ValueError("invalid funding holding horizon")
        horizon_ms = math.ceil(max_holding_s * 1000) + _BUFFER_MS
        schedules = self.snapshot()
        for venue in _VENUES:
            schedule = schedules.get(venue)
            if not schedule or "next_ms" not in schedule:
                return f"funding_schedule_missing:{venue}"
            if schedule.get("error"):
                return f"funding_schedule_unknown:{venue}"
            if not 0 <= now_ms - schedule["fetched_ms"] <= _MAX_AGE_MS:
                return f"funding_schedule_stale:{venue}"
            remaining = schedule["next_ms"] - now_ms
            if remaining <= 0:
                return f"funding_schedule_passed:{venue}"
            if remaining <= horizon_ms:
                return f"funding_settlement_approaching:{venue}"
        return None
