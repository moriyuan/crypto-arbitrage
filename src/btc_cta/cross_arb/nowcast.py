"""Causal L2/trade features and a labelled, non-executable visible-depth projection.

Public feeds contain aggregated price levels, not individual limit orders. Differences
are signed visible-depth changes: they cannot identify adds, cancels, or hidden fills.
Trade depletion is a hypothesis only: public trades and books have no joint event
sequence, timestamps have millisecond ambiguity, and a trade may meet replenished or
hidden liquidity. A projected Book retains the confirmed book's original timestamps.
"""

from __future__ import annotations

import math
from collections import OrderedDict, deque
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .market import Book, MarketDataError, _integer
from .stream import _BookParser, validate_symbol

ZERO = Decimal(0)
TAUS = (100, 500)
HISTORY_MS = 5000


@dataclass(frozen=True)
class _Trade:
    trade_id: str
    timestamp: int
    received: int
    price: Decimal
    quantity: Decimal
    buy: bool
    excluded: bool

    def signature(self) -> tuple[int, Decimal, Decimal, bool, bool]:
        return self.timestamp, self.price, self.quantity, self.buy, self.excluded


def _excluded(item: dict[str, Any], venue: str) -> bool:
    if venue == "bybit":
        # Absent flags are not affirmative hidden/block indicators. A present
        # malformed value is ambiguous and cannot deplete visible liquidity.
        return any(key in item and item[key] is not False for key in ("BT", "RPI"))
    return "isRPI" in item and item["isRPI"] != "no"


def _mid(book: Book) -> Decimal:
    return (book.bids[0][0] + book.asks[0][0]) / 2


def _imbalance(book: Book, levels: int) -> float:
    bid = sum((q for _, q in book.bids[:levels]), ZERO)
    ask = sum((q for _, q in book.asks[:levels]), ZERO)
    return float((bid - ask) / (bid + ask))


def _depth_change(previous: Book, current: Book) -> float:
    """Top-five signed depth change, excluding the moving far-depth boundary."""
    change = ZERO
    for old, new, sign in ((previous.bids[:5], current.bids[:5], 1),
                           (previous.asks[:5], current.asks[:5], -1)):
        old_map, new_map = dict(old), dict(new)
        boundary = max(old[-1][0], new[-1][0]) if sign == 1 else min(old[-1][0], new[-1][0])
        for price in old_map.keys() | new_map.keys():
            if (price >= boundary) if sign == 1 else (price <= boundary):
                change += sign * (new_map.get(price, ZERO) - old_map.get(price, ZERO))
    return float(change)


class FlowState:
    """One connection's bounded, arrival-causal aggregate depth and aggressor flow.

    Call reset after every disconnect or parser failure; the next book must be a
    snapshot. consume rejects backwards local receipt times. features refuses a
    decision before the most recent accepted arrival. Trade windows use source T,
    not receipt time, so delayed batches or Bitget startup history are not new flow.

    numeric values are fixed-name floats for chronological model fitting. OFI is
    an exponentially decayed quantity impulse (base-asset units); mid velocity is its analogous
    price-change impulse (bp/s). Both decay on local arrival time. Visible-depletion
    eligibility additionally requires book source T < trade source T <= decision T.
    Equal source timestamps are conservatively treated as already covered.
    """

    def __init__(self, venue: str, *, symbol: str = "BTCUSDT", max_trades: int = 10000,
                 max_seen: int = 50000) -> None:
        if venue not in {"bybit", "bitget"}:
            raise ValueError("unsupported venue")
        for limit in (max_trades, max_seen):
            if type(limit) is not int or not 1 <= limit <= 1000000:
                raise ValueError("invalid flow history capacity")
        self.venue = venue
        self.symbol = validate_symbol(symbol)
        self.max_trades = max_trades
        self.max_seen = max_seen
        self.reset()

    def reset(self) -> None:
        self._parser = _BookParser(self.venue, self.symbol)
        self._book: Book | None = None
        self._received = 0
        self._trades: deque[_Trade] = deque()
        self._seen: OrderedDict[str, _Trade] = OrderedDict()
        self._retired_ts = 0
        self._lost_until = 0
        self._ofi = {tau: 0.0 for tau in TAUS}
        self._velocity = {tau: 0.0 for tau in TAUS}
        self._flow_time = 0
        self._counters = {"accepted_trades": 0, "duplicate_trades": 0,
                          "excluded_trades": 0, "retired_trades": 0,
                          "evicted_trades": 0, "book_updates": 0}

    def _is_trade(self, payload: dict[str, Any]) -> bool:
        if self.venue == "bybit":
            return payload.get("topic") == f"publicTrade.{self.symbol}"
        arg = payload.get("arg")
        return (isinstance(arg, dict) and arg.get("topic") == "publicTrade"
                and payload.get("event") != "subscribe")

    def consume(self, payload: dict[str, Any], received_at_ms: int) -> bool:
        received = _integer(received_at_ms, "receipt timestamp", 1, 2**63 - 1)
        if received < self._received:
            raise MarketDataError("backwards receipt timestamp")
        if not isinstance(payload, dict):
            raise MarketDataError("invalid flow payload")
        if self._is_trade(payload):
            return self._consume_trades(payload, received)
        book = self._parser.parse(payload, received)
        if book is None:
            return False
        if self._book is not None:
            ofi = _depth_change(self._book, book)
            mid_change_bps = float((_mid(book) / _mid(self._book) - 1) * 10000)
            for tau in TAUS:
                decay = math.exp(-(received - self._flow_time) / tau)
                self._ofi[tau] = self._ofi[tau] * decay + ofi
                self._velocity[tau] = (self._velocity[tau] * decay
                                       + mid_change_bps * 1000 / tau)
        self._book = book
        self._flow_time = received
        self._received = received
        self._counters["book_updates"] += 1
        self._prune(received)
        return True

    def _consume_trades(self, payload: dict[str, Any], received: int) -> bool:
        # Validate the whole public batch before accepting any individual member.
        # Parser trade counting is diagnostic only; our own state commits atomically.
        self._parser.parse(payload, received)
        staged: OrderedDict[str, _Trade] = OrderedDict()
        duplicates = retired = 0
        for item in payload["data"]:
            trade = _Trade(item["i"], int(item["T"]), received, Decimal(item["p"]),
                           Decimal(item["v"]), item["S"].lower() == "buy",
                           _excluded(item, self.venue))
            previous = staged.get(trade.trade_id, self._seen.get(trade.trade_id))
            if previous is not None:
                if previous.signature() != trade.signature():
                    raise MarketDataError("conflicting duplicate trade id")
                duplicates += 1
            elif trade.timestamp <= self._retired_ts:
                # A bounded dedup cache cannot prove an older ID is unseen.
                retired += 1
            else:
                staged[trade.trade_id] = trade
        self._counters["duplicate_trades"] += duplicates
        self._counters["retired_trades"] += retired
        for trade in staged.values():
            self._trades.append(trade)
            self._seen[trade.trade_id] = trade
            self._counters["accepted_trades"] += 1
            self._counters["excluded_trades"] += int(trade.excluded)
        while len(self._seen) > self.max_seen:
            _, removed = self._seen.popitem(last=False)
            self._retired_ts = max(self._retired_ts, removed.timestamp)
        self._received = received
        self._prune(received)
        return bool(staged)

    def _prune(self, received: int) -> None:
        while self._trades and (len(self._trades) > self.max_trades
                                or self._trades[0].received <= received - HISTORY_MS):
            removed = self._trades.popleft()
            self._counters["evicted_trades"] += 1
            if (not removed.excluded and
                    (self._book is None or removed.timestamp > self._book.exchange_ts_ms)):
                self._lost_until = max(self._lost_until, removed.timestamp)

    def features(self, now_ms: int) -> dict[str, Any] | None:
        now = _integer(now_ms, "decision timestamp", 1, 2**63 - 1)
        book = self._book
        if book is None or now < self._received:
            return None
        midpoint = _mid(book)
        bid, bid_qty = book.bids[0]
        ask, ask_qty = book.asks[0]
        microprice = (ask * bid_qty + bid * ask_qty) / (bid_qty + ask_qty)
        numeric = {
            "spread_bps": float((ask - bid) / midpoint * 10000),
            "microprice_offset_bps": float((microprice / midpoint - 1) * 10000),
            "imbalance_top1": _imbalance(book, 1),
            "imbalance_top5": _imbalance(book, 5),
            "book_age_ms": float(max(0, now - book.exchange_ts_ms)),
            "receive_age_ms": float(now - book.received_at_ms),
            "transport_delay_ms": float(book.received_at_ms - book.exchange_ts_ms),
        }
        for tau in TAUS:
            decay = math.exp(-(now - self._flow_time) / tau)
            numeric[f"depth_ofi_{tau}ms"] = self._ofi[tau] * decay
            numeric[f"mid_velocity_{tau}ms_bps_s"] = self._velocity[tau] * decay
            recent = [trade for trade in self._trades if now - tau < trade.timestamp <= now]
            numeric[f"signed_trade_qty_{tau}ms"] = float(sum(
                (trade.quantity * (1 if trade.buy else -1) for trade in recent), ZERO))
            numeric[f"trade_count_{tau}ms"] = float(len(recent))
        recent_trades = [trade for trade in self._trades if now - 500 < trade.timestamp <= now]
        latest = max(recent_trades, key=lambda t: (t.timestamp, t.received, t.trade_id),
                     default=None)
        numeric["last_trade_offset_bps"] = (
            float((latest.price / midpoint - 1) * 10000) if latest is not None else 0.0)
        numeric["last_trade_age_ms"] = float(now - latest.timestamp) if latest else 500.0
        pending = [trade for trade in self._trades
                   if not trade.excluded and book.exchange_ts_ms < trade.timestamp <= now]
        numeric["unreflected_buy_qty"] = float(sum(
            (t.quantity for t in pending if t.buy), ZERO))
        numeric["unreflected_sell_qty"] = float(sum(
            (t.quantity for t in pending if not t.buy), ZERO))
        lost = self._lost_until > book.exchange_ts_ms
        projected, unmatched = self._project(book, pending) if not lost else (None, ZERO)
        projected_mid = _mid(projected) if projected else None
        numeric["projection_available"] = float(projected is not None)
        numeric["projected_mid_offset_bps"] = (
            float((projected_mid / midpoint - 1) * 10000) if projected_mid is not None else 0.0)
        return {
            "symbol": self.symbol,
            "confirmed_book": book,
            "projected_book": projected,
            "book_ts_ms": book.exchange_ts_ms,
            "book_received_ms": book.received_at_ms,
            "received_ms": self._received,
            "midpoint": float(midpoint),
            "bestbid": float(bid),
            "bestask": float(ask),
            "microprice": float(microprice),
            "projected_midpoint": float(projected_mid) if projected_mid is not None else None,
            "numeric": numeric,
            "diagnostics": {
                **self._counters,
                "buffered_trades": len(self._trades),
                "dedup_ids": len(self._seen),
                "retired_source_watermark_ms": self._retired_ts,
                "pending_history_lost": lost,
                "unmatched_depletion_qty": float(unmatched),
                "projection_is_executable": False,
            },
        }

    @staticmethod
    def _project(book: Book, pending: list[_Trade]) -> tuple[Book | None, Decimal]:
        bids, asks = dict(book.bids), dict(book.asks)
        unmatched = ZERO
        # Exact-price subtraction commutes, including out-of-order trade arrivals.
        for trade in pending:
            side = asks if trade.buy else bids
            available = side.get(trade.price, ZERO)
            consumed = min(available, trade.quantity)
            remaining = available - consumed
            unmatched += trade.quantity - consumed
            if remaining:
                side[trade.price] = remaining
            else:
                side.pop(trade.price, None)
        if not bids or not asks:
            return None, unmatched
        return Book(book.venue, tuple(bids.items()), tuple(asks.items()),
                    book.received_at_ms, book.exchange_ts_ms, book.request_ms), unmatched
