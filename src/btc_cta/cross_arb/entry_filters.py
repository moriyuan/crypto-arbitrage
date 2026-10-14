"""Receipt-time volatility and near-touch liquidity gates for new paper entries.

Only completed, contiguous sampling buckets contribute to realized volatility.
The caller validates quote freshness and resets this state on stream invalidation.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Any

from .market import Book

_VENUES = ("bybit", "bitget")
_BPS = Decimal(10000)
_MAX_RETURNS = 10000


@dataclass(frozen=True)
class EntryFilterConfig:
    volatility_window_s: int = 60
    volatility_sample_ms: int = 1000
    min_volatility_bps: Decimal = Decimal(5)
    liquidity_band_bps: Decimal = Decimal(10)
    min_depth_usdt: Decimal = Decimal(500)

    def __post_init__(self) -> None:
        if (
            type(self.volatility_window_s) is not int
            or type(self.volatility_sample_ms) is not int
            or self.volatility_window_s <= 0
            or self.volatility_sample_ms <= 0
        ):
            raise ValueError("volatility window and sample must be positive integers")
        duration = self.volatility_window_s * 1000
        if duration % self.volatility_sample_ms or not (
            2 <= duration // self.volatility_sample_ms <= _MAX_RETURNS
        ):
            raise ValueError("volatility window must contain 2 to 10000 whole samples")
        for value in (self.min_volatility_bps, self.liquidity_band_bps, self.min_depth_usdt):
            if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                raise ValueError("entry thresholds must be finite nonnegative Decimal values")
        if not 0 < self.liquidity_band_bps < _BPS:
            raise ValueError("liquidity band must be greater than 0 and less than 10000 bps")


class EntryFilters:
    def __init__(self, config: EntryFilterConfig):
        self.config = config
        self.required_samples = config.volatility_window_s * 1000 // config.volatility_sample_ms
        self._closed: deque[tuple[int, dict[str, Decimal]]] = deque(
            maxlen=self.required_samples + 1
        )
        self.reset()

    def reset(self) -> None:
        self._closed.clear()
        self._pending: tuple[int, dict[str, Decimal]] | None = None
        self._last_call_ms: int | None = None
        self._last_valid_ms: int | None = None
        self._cached_volatility: dict[str, Decimal] | None = None

    @staticmethod
    def _paired(books: dict[str, Book] | None) -> bool:
        return books is not None and all(
            venue in books and isinstance(books[venue], Book) and books[venue].venue == venue
            for venue in _VENUES
        )

    def observe(self, books: dict[str, Book] | None, now_ms: int) -> None:
        if type(now_ms) is not int or now_ms < 0:
            raise ValueError("observation clock must be a nonnegative integer")
        sample_ms = self.config.volatility_sample_ms
        if (self._last_call_ms is not None and now_ms < self._last_call_ms) or (
            self._last_valid_ms is not None and now_ms - self._last_valid_ms > sample_ms
        ):
            self.reset()
        self._last_call_ms = now_ms
        if books is not None and not self._paired(books):
            self.reset()
            self._last_call_ms = now_ms
            return
        bucket = now_ms // sample_ms
        if self._pending is not None and bucket > self._pending[0]:
            if bucket != self._pending[0] + 1:
                self.reset()
                self._last_call_ms = now_ms
            else:
                self._closed.append(self._pending)
                self._cached_volatility = None
                self._pending = None
        if books is None:
            return
        # A timer may close a bucket without a new quote. Never create a quote
        # for the next bucket until a fresh paired book is actually observed.
        if self._closed and self._closed[-1][0] != bucket - 1:
            self.reset()
            self._last_call_ms = now_ms
        with localcontext() as context:
            context.prec = 50
            mids = {v: (books[v].bids[0][0] + books[v].asks[0][0]) / 2 for v in _VENUES}
        self._pending = bucket, mids
        self._last_valid_ms = now_ms

    def _volatility(self) -> dict[str, Decimal] | None:
        if len(self._closed) < self.required_samples + 1:
            return None
        if self._cached_volatility is None:
            with localcontext() as context:
                context.prec = 50
                mids = list(self._closed)
                self._cached_volatility = {
                    venue: _BPS
                    * sum(
                        (
                            (current[1][venue] / previous[1][venue]).ln() ** 2
                            for previous, current in zip(mids, mids[1:], strict=False)
                        ),
                        Decimal(0),
                    ).sqrt()
                    for venue in _VENUES
                }
        return self._cached_volatility

    def evaluate(self, books: dict[str, Book] | None, long: str, short: str) -> dict[str, Any]:
        volatility = self._volatility()
        row: dict[str, Any] = {
            "entry_filter_passed": False,
            "entry_filter_reason": "entry_filter_missing_feed",
            "bybit_volatility_bps": str(volatility["bybit"]) if volatility else None,
            "bitget_volatility_bps": str(volatility["bitget"]) if volatility else None,
            "long_depth_usdt": None,
            "short_depth_usdt": None,
            "volatility_samples": max(0, len(self._closed) - 1),
            "volatility_required_samples": self.required_samples,
        }
        if books is None or not self._paired(books) or {long, short} != set(_VENUES):
            return row
        with localcontext() as context:
            context.prec = 50
            buy_limit = books[long].asks[0][0] * (1 + self.config.liquidity_band_bps / _BPS)
            sell_limit = books[short].bids[0][0] * (1 - self.config.liquidity_band_bps / _BPS)
            long_depth = sum(
                (price * quantity for price, quantity in books[long].asks if price <= buy_limit),
                Decimal(0),
            )
            short_depth = sum(
                (price * quantity for price, quantity in books[short].bids if price >= sell_limit),
                Decimal(0),
            )
        row.update(long_depth_usdt=str(long_depth), short_depth_usdt=str(short_depth))
        if self.config.min_volatility_bps > 0 and volatility is None:
            reason = "entry_filter_warmup"
        elif (
            self.config.min_volatility_bps > 0
            and volatility is not None
            and any(value < self.config.min_volatility_bps for value in volatility.values())
        ):
            reason = "volatility_below_threshold"
        elif min(long_depth, short_depth) < self.config.min_depth_usdt:
            reason = "liquidity_below_threshold"
        else:
            reason = "entry_filter_passed"
        row.update(entry_filter_passed=reason == "entry_filter_passed", entry_filter_reason=reason)
        return row
