"""Public USDT perpetual WebSocket books and unmodified trade frames.

Bybit: /docs/v5/websocket/public/orderbook (50 levels, 20 ms snapshot/delta).
Bitget: v3 books supports full-depth snapshot/delta; books50 is the legacy default.
Observed channel behavior is recorded separately; a subscription name is not a guarantee.
Both subscriptions also include publicTrade. No authentication or trading APIs.
"""

from __future__ import annotations

import json
import math
import re
import threading
import time
from collections import deque
from collections.abc import Callable
from decimal import Decimal
from types import TracebackType
from typing import Any

from websockets.sync.client import connect
from websockets.sync.connection import Connection

from .market import Book, Levels, MarketDataError, _decimal, _integer, _object, _proxy_url

VENUES = ("bybit", "bitget")
URLS = {
    "bybit": "wss://stream.bybit.com/v5/public/linear",
    "bitget": "wss://ws.bitget.com/v3/ws/public",
}

def validate_symbol(value: object) -> str:
    """Validate an explicit USDT contract identifier; this does not prove listing."""
    if not isinstance(value, str) or re.fullmatch(r"[A-Z0-9]{2,24}USDT", value) is None:
        raise MarketDataError("invalid USDT perpetual symbol")
    return value


def _depth_channel(value: object) -> str:
    if not isinstance(value, str) or value not in {"books", "books50"}:
        raise MarketDataError("invalid Bitget depth channel")
    return value


def subscriptions_for(
    symbol: str, bitget_depth_channel: str = "books50",
) -> dict[str, dict[str, Any]]:
    symbol = validate_symbol(symbol)
    bitget_depth_channel = _depth_channel(bitget_depth_channel)
    return {
        "bybit": {"op": "subscribe",
                  "args": [f"orderbook.50.{symbol}", f"publicTrade.{symbol}"]},
        "bitget": {
            "op": "subscribe",
            "args": [
                {"instType": "usdt-futures", "topic": topic, "symbol": symbol}
                for topic in (bitget_depth_channel, "publicTrade")
            ],
        },
    }


# Retained for readers of older BTC archives. Live instances generate their own.
SUBSCRIPTIONS = subscriptions_for("BTCUSDT")
MessageCallback = Callable[[str, int, dict[str, Any]], None]


def _timestamp(value: object) -> int:
    return _integer(value, "stream timestamp", 1, 2**63 - 1)


def _rows(value: object, *, delta: bool = False, maximum: int = 100) -> Levels:
    if not isinstance(value, list) or len(value) > maximum or (not value and not delta):
        raise MarketDataError("invalid stream depth")
    result: list[tuple[Decimal, Decimal]] = []
    seen: set[Decimal] = set()
    for row in value:
        if not isinstance(row, list) or len(row) != 2:
            raise MarketDataError("invalid stream level")
        price = _decimal(row[0], "price")
        quantity = _decimal(row[1], "quantity", allow_zero=delta)
        if price in seen:
            raise MarketDataError("duplicate stream price")
        seen.add(price)
        result.append((price, quantity))
    return tuple(result)


class _BookParser:
    """Connection-local state: a new connection always requires a fresh snapshot."""

    def __init__(self, venue: str, symbol: str = "BTCUSDT") -> None:
        if venue not in VENUES:
            raise MarketDataError("unsupported stream venue")
        self.venue = venue
        self.symbol = validate_symbol(symbol)
        self.bids: dict[Decimal, Decimal] = {}
        self.asks: dict[Decimal, Decimal] = {}
        self.seq: int | None = None
        self.update_id: int | None = None
        self.timestamp: int | None = None
        self.trade_ticks = 0
        self._bitget_first_update = False
        self._bitget_depth_topic: str | None = None
        self._bitget_max_depth = 1000

    def parse(self, payload: dict[str, Any], received: int) -> Book | None:
        if self.venue == "bybit":
            return self._bybit(payload, received)
        return self._bitget(payload, received)

    def _trades(self, value: object, *, bybit: bool) -> None:
        if not isinstance(value, list) or not 1 <= len(value) <= 1024:
            raise MarketDataError("invalid public trade batch")
        for item in value:
            item = _object(item, "public trade")
            if bybit and item.get("s") != self.symbol:
                raise MarketDataError("wrong public trade symbol")
            if any(key in item and item[key] != self.symbol for key in ("s", "symbol")):
                raise MarketDataError("conflicting public trade symbol")
            # IDs and every member of a batch remain in the callback's original payload.
            if not isinstance(item.get("i"), str) or not 1 <= len(item["i"]) <= 256:
                raise MarketDataError("invalid public trade id")
            _timestamp(item.get("T"))
            _decimal(item.get("p"), "trade price")
            _decimal(item.get("v"), "trade quantity")
            if item.get("S") not in (("Buy", "Sell") if bybit else ("buy", "sell")):
                raise MarketDataError("invalid public trade side")
        self.trade_ticks += len(value)

    def _bybit(self, payload: dict[str, Any], received: int) -> Book | None:
        if "op" in payload:
            if payload.get("success") is False:
                raise MarketDataError("bybit subscription rejected")
            if payload.get("op") not in {"subscribe", "ping", "pong"}:
                raise MarketDataError("unexpected bybit control frame")
            return None
        topic = payload.get("topic")
        if topic == f"publicTrade.{self.symbol}":
            self._trades(payload.get("data"), bybit=True)
            return None
        if topic != f"orderbook.50.{self.symbol}":
            raise MarketDataError("unexpected bybit topic")
        data = _object(payload.get("data"), "bybit stream data")
        if (data.get("s") != self.symbol
                or ("symbol" in data and data["symbol"] != self.symbol)):
            raise MarketDataError("wrong bybit symbol")
        kind = payload.get("type")
        if kind not in {"snapshot", "delta"}:
            raise MarketDataError("unexpected bybit book type")
        seq = _integer(data.get("seq"), "sequence", 0, 2**63 - 1)
        update_id = _integer(data.get("u"), "update id", 1, 2**63 - 1)
        timestamp = _timestamp(payload.get("cts"))
        reset = kind == "snapshot" or update_id == 1
        if self.timestamp is not None and timestamp < self.timestamp:
            raise MarketDataError("out-of-order bybit timestamp")
        if not reset:
            if self.seq is None or self.update_id is None:
                raise MarketDataError("bybit delta before snapshot")
            # Level-50 IDs need only increase; unlike full-depth IDs, gaps are normal.
            if seq <= self.seq or update_id <= self.update_id:
                raise MarketDataError("out-of-order bybit update")
        bids = dict() if reset else self.bids.copy()
        asks = dict() if reset else self.asks.copy()
        for target, rows in ((bids, data.get("b")), (asks, data.get("a"))):
            for price, quantity in _rows(rows, delta=not reset):
                if quantity == 0:
                    target.pop(price, None)
                else:
                    target[price] = quantity
        bid_rows = tuple(sorted(bids.items(), reverse=True)[:50])
        ask_rows = tuple(sorted(asks.items())[:50])
        book = Book("bybit", bid_rows, ask_rows, received, timestamp, 0.0)
        self.bids, self.asks = dict(bid_rows), dict(ask_rows)
        self.seq, self.update_id, self.timestamp = seq, update_id, timestamp
        return book

    def _bitget(self, payload: dict[str, Any], received: int) -> Book | None:
        if payload.get("event") == "error":
            raise MarketDataError("bitget subscription rejected")
        arg = _object(payload.get("arg"), "bitget stream argument")
        if arg.get("instType") != "usdt-futures" or arg.get("symbol") != self.symbol:
            raise MarketDataError("wrong bitget product")
        topic = arg.get("topic")
        if topic not in {"books", "books5", "books50", "publicTrade"}:
            raise MarketDataError("unexpected bitget topic")
        if payload.get("event") == "subscribe":
            return None
        if topic == "publicTrade":
            self._trades(payload.get("data"), bybit=False)
            return None
        action = payload.get("action")
        if action not in {"snapshot", "update"}:
            raise MarketDataError("unexpected bitget book action")
        if topic == "books5" and action != "snapshot":
            raise MarketDataError("bitget books5 requires full snapshot")
        data = payload.get("data")
        if not isinstance(data, list) or len(data) != 1:
            raise MarketDataError("invalid bitget book batch")
        row = _object(data[0], "bitget book")
        if any(key in row and row[key] != self.symbol for key in ("s", "symbol")):
            raise MarketDataError("conflicting bitget book symbol")
        seq = _integer(row.get("seq"), "sequence", 0, 2**63 - 1)
        timestamp = _timestamp(row.get("ts"))
        if self.timestamp is not None and timestamp < self.timestamp:
            raise MarketDataError("out-of-order bitget timestamp")
        if topic == "books5":
            # Archived books5 runs remain replayable with their original strict ordering.
            if self.seq is not None and seq <= self.seq:
                raise MarketDataError("out-of-order bitget sequence")
            book = Book(
                "bitget", _rows(row.get("b"), maximum=5), _rows(row.get("a"), maximum=5),
                received, timestamp, 0.0,
            )
            self.bids, self.asks = dict(book.bids), dict(book.asks)
            self.seq, self.timestamp = seq, timestamp
            self._bitget_first_update = False
            self._bitget_depth_topic = topic
            return book
        pseq = _integer(row.get("pseq"), "previous sequence", 0, 2**63 - 1)
        reset = action == "snapshot"
        max_depth = 1000 if reset else self._bitget_max_depth
        if topic == "books" and "maxDepth" in row:
            max_depth = _integer(row["maxDepth"], "Bitget maximum depth", 1, 1000)
        if not reset:
            if self.seq is None:
                raise MarketDataError("bitget update before snapshot")
            if self._bitget_depth_topic != topic:
                raise MarketDataError("bitget depth channel changed without snapshot")
            if seq <= pseq:
                raise MarketDataError("nonincreasing bitget sequence range")
            if self._bitget_first_update:
                if not pseq <= self.seq <= seq:
                    raise MarketDataError("bitget first update sequence gap")
            elif pseq != self.seq or seq <= self.seq:
                # A pseq=0 service reset also requires a new snapshot/reconnect.
                raise MarketDataError("bitget update sequence gap")
        bids = {} if reset else self.bids.copy()
        asks = {} if reset else self.asks.copy()
        for target, rows in ((bids, row.get("b")), (asks, row.get("a"))):
            for price, quantity in _rows(rows, delta=not reset,
                                         maximum=1000 if topic == "books" else 100):
                if quantity == 0:
                    target.pop(price, None)
                else:
                    # Exchange updates carry the current absolute size, not a size delta.
                    target[price] = quantity
        if topic == "books":
            if len(bids) > max_depth or len(asks) > max_depth:
                raise MarketDataError("bitget reconstructed depth exceeds maximum")
            # Keep the full known state: deleting a top-50 level must promote the
            # previously observed deeper level, without inventing or losing it.
            full = Book("bitget", tuple(sorted(bids.items(), reverse=True)),
                        tuple(sorted(asks.items())), received, timestamp, 0.0)
            bid_rows, ask_rows = full.bids[:50], full.asks[:50]
            book = Book("bitget", bid_rows, ask_rows, received, timestamp, 0.0)
        else:
            bid_rows = tuple(sorted(bids.items(), reverse=True)[:50])
            ask_rows = tuple(sorted(asks.items())[:50])
            book = Book("bitget", bid_rows, ask_rows, received, timestamp, 0.0)
            bids, asks = dict(bid_rows), dict(ask_rows)
        # Commit only after the entire reconstruction and both bounds validate.
        self.bids, self.asks = bids, asks
        self.seq, self.timestamp = seq, timestamp
        self._bitget_first_update = reset
        self._bitget_depth_topic = topic
        self._bitget_max_depth = max_depth
        return book


class StreamBooks:
    """Two receiver threads; immutable snapshots plus loss-intolerant update FIFO.

    ``on_message(venue, received_at_ms, payload)`` runs on both receiver threads,
    before validation, for every decoded JSON frame. The callback must be fast and
    thread-safe; exceptions stop both feeds. All trade batch members are preserved.
    ``next_update`` returns each book update after both venues are initialized;
    a disconnect discards queued pairs because one leg's current state is unknown.
    ``event_mode=True`` instead enables ``next_event`` for validated book/trade
    payloads and connection invalidations, without accumulating unused book pairs.
    Events have kind, venue, received_at_ms, generation and payload (None for an
    invalidation). A generation starts at zero and increases after invalidation.
    Disconnect purges only that venue's pending events: keeping the other venue's
    deltas intact is necessary for its consumer's reconstructed book.
    Consumers must apply their own source-age and inter-venue-skew guards.
    """

    def __init__(
        self,
        proxy: str | None = "http://127.0.0.1:7897",
        *,
        symbol: str = "BTCUSDT",
        bitget_depth_channel: str = "books50",
        on_message: MessageCallback | None = None,
        max_updates: int = 10000,
        event_mode: bool = False,
    ) -> None:
        self.proxy = _proxy_url(proxy)
        self.symbol = validate_symbol(symbol)
        self.bitget_depth_channel = _depth_channel(bitget_depth_channel)
        self._subscriptions = subscriptions_for(self.symbol, self.bitget_depth_channel)
        if type(max_updates) is not int or not 1 <= max_updates <= 100000:
            raise MarketDataError("invalid stream queue capacity")
        if type(event_mode) is not bool:
            raise MarketDataError("invalid stream event mode")
        self._event_mode = event_mode
        self._events: deque[dict[str, Any]] = deque()
        self._generation = {venue: 0 for venue in VENUES}
        self._invalidated: set[str] = set()
        self._last_received = 0
        self._dropped_events = 0
        self._purged_events = 0
        self._on_message = on_message
        self._max_updates = max_updates
        self._condition = threading.Condition()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._connections: dict[str, Connection] = {}
        self._books: dict[str, Book] = {}
        self._updates: deque[dict[str, Book]] = deque()
        self._fatal_error: str | None = None
        self._dropped_updates = 0
        self._started = False
        self._stats: dict[str, dict[str, Any]] = {
            venue: {
                "connected": False, "frames": 0, "book_updates": 0, "trade_frames": 0,
                "trade_ticks": 0, "disconnects": 0, "invalidations": 0, "last_received_at_ms": None,
                "last_book_at_ms": None, "last_error": None,
            }
            for venue in VENUES
        }

    def __enter__(self) -> StreamBooks:
        if self._started or self._stop.is_set():
            raise RuntimeError("stream cannot be restarted")
        self._started = True
        for venue in VENUES:
            thread = threading.Thread(target=self._run, args=(venue,), daemon=True,
                                      name=f"cross-arb-{venue}")
            self._threads.append(thread)
            thread.start()
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 traceback: TracebackType | None) -> None:
        self.close()

    def close(self) -> None:
        self._stop.set()
        with self._condition:
            self._books.clear()
            self._updates.clear()
            self._events.clear()
            connections = tuple(self._connections.values())
            self._condition.notify_all()
        for connection in connections:
            connection.close()
        for thread in self._threads:
            if thread is not threading.current_thread():
                thread.join(timeout=12)

    def snapshot(self) -> dict[str, Book] | None:
        with self._condition:
            if self._stop.is_set() or len(self._books) != 2:
                return None
            return self._books.copy()

    def next_update(self, timeout: float = 0.5) -> dict[str, Book] | None:
        if self._event_mode:
            raise RuntimeError("next_update is unavailable in event mode")
        if not math.isfinite(timeout) or not 0 <= timeout <= 60:
            raise ValueError("invalid stream update timeout")
        with self._condition:
            self._condition.wait_for(lambda: bool(self._updates) or self._stop.is_set(), timeout)
            if self._stop.is_set() or not self._updates:
                return None
            return self._updates.popleft()

    def next_event(self, timeout: float = 0.5) -> dict[str, Any] | None:
        if not self._event_mode:
            raise RuntimeError("next_event requires event_mode=True")
        if not math.isfinite(timeout) or not 0 <= timeout <= 60:
            raise ValueError("invalid stream event timeout")
        with self._condition:
            self._condition.wait_for(lambda: bool(self._events) or self._stop.is_set(), timeout)
            if self._stop.is_set() or not self._events:
                return None
            return self._events.popleft()

    def stats(self) -> dict[str, Any]:
        with self._condition:
            return {
                **{venue: row.copy() for venue, row in self._stats.items()},
                "fatal_error": self._fatal_error,
                "queued_updates": len(self._updates),
                "dropped_updates": self._dropped_updates,
                "queued_events": len(self._events),
                "dropped_events": self._dropped_events,
                "purged_events": self._purged_events,
                "event_mode": self._event_mode,
                "symbol": self.symbol,
                "bitget_depth_channel": self.bitget_depth_channel,
                "book_channels": {"bybit": "orderbook.50",
                                  "bitget": self.bitget_depth_channel},
            }

    def _fatal(self, reason: str) -> None:
        with self._condition:
            self._fatal_error = reason
            self._books.clear()
            self._updates.clear()
            self._events.clear()
            self._stop.set()
            self._condition.notify_all()

    def _invalidate(self, venue: str, error: str | None = None) -> None:
        with self._condition:
            self._books.pop(venue, None)
            self._updates.clear()
            self._connections.pop(venue, None)
            if venue not in self._invalidated:
                self._invalidated.add(venue)
                self._generation[venue] += 1
                self._stats[venue]["invalidations"] += 1
                if self._event_mode and not self._stop.is_set():
                    before = len(self._events)
                    self._events = deque(e for e in self._events if e["venue"] != venue)
                    self._purged_events += before - len(self._events)
                    self._enqueue_event(venue, "invalidate", self._receive_time(), None)
            row = self._stats[venue]
            row["connected"] = False
            if error is not None:
                row["disconnects"] += 1
                row["last_error"] = error
            self._condition.notify_all()

    def _publish(self, venue: str, book: Book) -> None:
        with self._condition:
            if self._stop.is_set():
                return
            self._books[venue] = book
            self._invalidated.discard(venue)
            row = self._stats[venue]
            row["book_updates"] += 1
            row["last_book_at_ms"] = book.received_at_ms
            if not self._event_mode and len(self._books) == 2:
                if len(self._updates) >= self._max_updates:
                    self._dropped_updates += 1
                    self._fatal("update_queue_overflow")
                    return
                self._updates.append(self._books.copy())
                self._condition.notify()

    def _receive_time(self) -> int:
        # Called under the publication lock. Clamp a wall-clock step backwards so
        # FIFO ordering never claims an earlier receipt time for a later event.
        self._last_received = max(self._last_received, time.time_ns() // 1000000)
        return self._last_received

    def _enqueue_event(
        self, venue: str, kind: str, received: int, payload: dict[str, Any] | None,
    ) -> None:
        if self._stop.is_set():
            return
        if len(self._events) >= self._max_updates:
            self._dropped_events += 1
            self._fatal("event_queue_overflow")
            return
        self._events.append({"kind": kind, "venue": venue, "received_at_ms": received,
                             "generation": self._generation[venue], "payload": payload})
        self._condition.notify_all()

    def _message(self, venue: str, parser: _BookParser, raw: str | bytes) -> None:
        if parser.venue != venue or parser.symbol != self.symbol:
            raise MarketDataError("stream parser identity mismatch")
        if raw == "pong" or raw == b"pong":
            return
        if len(raw) > 1024 * 1024:
            raise MarketDataError("oversized stream frame")
        payload = _object(json.loads(raw), "stream frame")
        # Timestamp, raw callback, validation and FIFO append share one lock so
        # two receiver threads cannot publish receipt timestamps out of order.
        with self._condition:
            if self._stop.is_set():
                return
            received = self._receive_time()
            if self._on_message is not None:
                try:
                    self._on_message(venue, received, payload)
                except Exception:
                    self._fatal("callback_failed")
                    return
            before_trades = parser.trade_ticks
            book = parser.parse(payload, received)
            is_trade = parser.trade_ticks != before_trades
            row = self._stats[venue]
            row["frames"] += 1
            row["last_received_at_ms"] = received
            if is_trade:
                row["trade_frames"] += 1
                row["trade_ticks"] += parser.trade_ticks - before_trades
            if book is not None:
                self._publish(venue, book)
            if self._event_mode and (book is not None or is_trade):
                self._enqueue_event(venue, "book" if book is not None else "trade",
                                    received, payload)

    def _run(self, venue: str) -> None:
        delay = 0.5
        while not self._stop.is_set():
            parser = _BookParser(venue, self.symbol)
            try:
                with connect(
                    URLS[venue], proxy=self.proxy, open_timeout=10, close_timeout=1,
                    ping_interval=None, max_size=1024 * 1024, max_queue=1024,
                ) as connection:
                    with self._condition:
                        if self._stop.is_set():
                            return
                        self._connections[venue] = connection
                        self._invalidated.discard(venue)
                        self._stats[venue]["connected"] = True
                    connection.send(json.dumps(self._subscriptions[venue], separators=(",", ":")))
                    last_ping = last_receive = time.monotonic()
                    while not self._stop.is_set():
                        now = time.monotonic()
                        if now - last_ping >= 20:
                            connection.send('{"op":"ping"}' if venue == "bybit" else "ping")
                            last_ping = now
                        if now - last_receive >= 30:
                            raise TimeoutError("public stream receive timeout")
                        try:
                            raw = connection.recv(timeout=0.5)
                        except TimeoutError:
                            continue
                        except Exception:
                            # Invalidate before the context manager's close handshake.
                            self._invalidate(venue)
                            raise
                        last_receive = time.monotonic()
                        try:
                            self._message(venue, parser, raw)
                        except Exception:
                            self._invalidate(venue)
                            raise
                        if parser.seq is not None:
                            delay = 0.5
            except Exception as exc:
                # Never include server-controlled error bodies or proxy settings in logs.
                self._invalidate(venue, type(exc).__name__)
            finally:
                self._invalidate(venue)
            if self._stop.wait(delay):
                break
            delay = min(delay * 2, 5.0)
