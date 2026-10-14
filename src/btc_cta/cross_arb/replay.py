"""Strict offline replay of one public incremental-book archive; no network calls."""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from .depth_sizing import DepthSizingConfig
from .engine import Policy, fresh
from .entry_filters import EntryFilterConfig
from .history_gate import HistoryGate
from .live import write_json
from .market import Book, Rules
from .paper import PaperSession
from .stream import _BookParser, validate_symbol

_VENUES = ("bybit", "bitget")
_CHANNELS = {"bybit": "orderbook.50", "bitget": "books"}
_MAX_GAP_MS = 5000


@dataclass(frozen=True)
class ReplayPoint:
    timestamp_ms: int
    books: dict[str, Book]
    reset: bool = False


@dataclass(frozen=True)
class ReplayData:
    points: list[ReplayPoint]
    audit: dict[str, Any]


def _reject_constant(value: str) -> None:
    raise ValueError("nonfinite JSON value")


def _json_object(text: str) -> dict[str, Any]:
    value = json.loads(text, parse_constant=_reject_constant)
    if not isinstance(value, dict):
        raise ValueError("expected JSON object")
    return value


def _run_metadata(run_dir: Path, symbol: str) -> tuple[dict[str, Any], dict[str, Any]]:
    metadata = _json_object((run_dir / "run_metadata.json").read_text())
    status = _json_object((run_dir / "status.json").read_text())
    for document in (metadata, status):
        if document.get("symbol") != symbol or document.get("book_channels") != _CHANNELS:
            raise ValueError("archive symbol or incremental channels mismatch")
        if document.get("execution_enabled") is not False:
            raise ValueError("source must be an explicit non-execution collection")
    if status.get("collection_status") != "COMPLETED" or status.get("error"):
        raise ValueError("source collection is incomplete")
    for key in ("stream_before_close", "stream"):
        stream = status.get(key)
        if not isinstance(stream, dict):
            raise ValueError("missing stream continuity evidence")
        if stream.get("fatal_error") or any(
            stream.get(k) != 0 for k in ("dropped_events", "dropped_updates", "purged_events")
        ):
            raise ValueError("source stream lost events")
        for venue in _VENUES:
            row = stream.get(venue)
            if not isinstance(row, dict) or row.get("disconnects") != 0:
                raise ValueError("source stream disconnected")
            allowed = (0,) if key == "stream_before_close" else (0, 1)
            if row.get("invalidations") not in allowed:
                raise ValueError("source stream invalidated")
    for venue in _VENUES:
        if status.get("events", {}).get(venue, {}).get("invalidate") != 0:
            raise ValueError("source archive has an unrecorded connection boundary")
    return metadata, status


def load_archive(run_dir: Path, symbol: str) -> ReplayData:
    symbol = validate_symbol(symbol)
    run_dir = Path(run_dir)
    _, status = _run_metadata(run_dir, symbol)
    archive = run_dir / "public-ws.jsonl.gz"
    before = archive.stat()
    digest = hashlib.sha256()
    with archive.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    parsers = {v: _BookParser(v, symbol=symbol) for v in _VENUES}
    books: dict[str, Book] = {}
    points: list[ReplayPoint] = []
    types = {"bybit": {"snapshot": 0, "delta": 0}, "bitget": {"snapshot": 0, "update": 0}}
    last_book: dict[str, int] = {}
    max_book_gap = dict.fromkeys(_VENUES, 0)
    frames = now = first = regressions = max_regression = max_gap = 0
    try:
        with gzip.open(archive, "rt", encoding="utf-8") as source:
            for line in source:
                frame = _json_object(line)
                venue, received, payload = (
                    frame.get("venue"),
                    frame.get("received_at_ms"),
                    frame.get("payload"),
                )
                if (
                    venue not in _VENUES
                    or type(received) is not int
                    or received <= 0
                    or not isinstance(payload, dict)
                ):
                    raise ValueError("invalid archive frame")
                if not frames:
                    first = received
                if received < now:
                    regressions += 1
                    max_regression = max(max_regression, now - received)
                    if now - received > 1000:
                        raise ValueError("large receipt clock reversal")
                elif now:
                    max_gap = max(max_gap, received - now)
                    if received - now > _MAX_GAP_MS:
                        raise ValueError("archive receipt gap exceeds five seconds")
                now = max(now, received)
                frames += 1
                parser = parsers[venue]
                topic = (
                    payload.get("topic")
                    if venue == "bybit"
                    else payload.get("arg", {}).get("topic")
                )
                is_book = topic == (f"orderbook.50.{symbol}" if venue == "bybit" else "books")
                if venue == "bitget" and topic not in ("books", "publicTrade"):
                    raise ValueError("unexpected Bitget archive channel")
                if is_book and ("type" in payload or "action" in payload):
                    kind = payload.get("type" if venue == "bybit" else "action")
                    if kind == "snapshot" and parser.seq is not None:
                        raise ValueError("second snapshot requires a separate replay segment")
                    if (
                        venue == "bybit"
                        and parser.seq is not None
                        and payload.get("data", {}).get("u") == 1
                    ):
                        raise ValueError("Bybit service reset inside archive")
                book = parser.parse(payload, received)
                if book is None:
                    continue
                if venue in last_book:
                    gap = now - last_book[venue]
                    max_book_gap[venue] = max(max_book_gap[venue], gap)
                    if gap > _MAX_GAP_MS:
                        raise ValueError("venue book gap exceeds five seconds")
                last_book[venue] = now
                kind = payload["type" if venue == "bybit" else "action"]
                types[venue][kind] += 1
                books[venue] = book
                if len(books) == 2:
                    points.append(ReplayPoint(now, books.copy()))
    except (OSError, EOFError, UnicodeError, KeyError, TypeError, AttributeError) as exc:
        raise ValueError("invalid or corrupt public archive") from exc
    after = archive.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("archive changed while being audited")
    if frames != status.get("raw_frames") or not points:
        raise ValueError("empty or inconsistent archive frame count")
    if not all(parsers[v].trade_ticks > 0 for v in _VENUES) or not (
        types["bybit"]["snapshot"] == types["bitget"]["snapshot"] == 1
        and types["bybit"]["delta"] > 0
        and types["bitget"]["update"] > 0
    ):
        raise ValueError("archive lacks paired trades or true incremental books")
    trades = {v: parsers[v].trade_ticks for v in _VENUES}
    return ReplayData(
        points,
        {
            "symbol": symbol,
            "first_ms": first,
            "last_ms": now,
            "archive_sha256": digest.hexdigest(),
            "raw_frames": frames,
            "points": len(points),
            "trade_ticks": trades,
            "trades": sum(trades.values()),
            "book_message_types": types,
            "receipt_regressions": regressions,
            "max_receipt_regression_ms": max_regression,
            "max_global_gap_ms": max_gap,
            "max_book_gap_ms": max_book_gap,
            "last_sequences": {v: parsers[v].seq for v in _VENUES},
            "sequence_errors": 0,
            "gaps": 0,
            "verified": True,
        },
    )


def run_segment(
    data: ReplayData,
    output: Path,
    rules: dict[str, Rules],
    policy: Policy,
    entry_filter: EntryFilterConfig,
    start_ms: int,
    end_ms: int,
    *,
    progress: Callable[[dict[str, Any]], None] | None = None,
    history_gate: HistoryGate | None = None,
    depth_sizing: DepthSizingConfig | None = None,
    allow_legacy_unfiltered: bool = False,
) -> dict[str, Any]:
    if history_gate is None and not allow_legacy_unfiltered:
        raise ValueError("historical ranking gate is required for replay")
    if history_gate is not None and not isinstance(history_gate, HistoryGate):
        raise ValueError("invalid historical ranking gate")
    if history_gate is not None and depth_sizing is None:
        depth_sizing = DepthSizingConfig()
    if depth_sizing is not None and not isinstance(depth_sizing, DepthSizingConfig):
        raise ValueError("invalid depth sizing configuration")
    audit = data.audit
    symbol = validate_symbol(audit.get("symbol"))
    if (
        audit.get("verified") is not True
        or re.fullmatch(r"[a-f0-9]{64}", str(audit.get("archive_sha256"))) is None
    ):
        raise ValueError("replay requires an audited archive")
    if (
        type(start_ms) is not int
        or type(end_ms) is not int
        or not (0 < audit["first_ms"] <= start_ms < end_ms <= audit["last_ms"] + 1)
    ):
        raise ValueError("segment boundaries must lie inside the audited receipt interval")
    if set(rules) != set(_VENUES) or not all(isinstance(r, Rules) for r in rules.values()):
        raise ValueError("both venues require explicit contract rules")
    if not isinstance(policy, Policy) or not isinstance(entry_filter, EntryFilterConfig):
        raise ValueError("invalid segment configuration")
    previous = 0
    for point in data.points:
        if (
            point.reset
            or point.timestamp_ms < previous
            or (previous and point.timestamp_ms - previous > _MAX_GAP_MS)
        ):
            raise ValueError("discontinuous replay points")
        if set(point.books) != set(_VENUES) or any(
            b.received_at_ms > point.timestamp_ms for b in point.books.values()
        ):
            raise ValueError("future or unpaired replay book")
        previous = point.timestamp_ms
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    session = PaperSession(
        output / "ledger.sqlite", policy, Decimal(1000), symbol=symbol, entry_filter=entry_filter,
        history_gate=history_gate, depth_sizing=depth_sizing,
    )
    warmup_start = (
        start_ms - entry_filter.volatility_window_s * 1000 - entry_filter.volatility_sample_ms
    )
    warmup = processed = 0
    selected = [p for p in data.points if start_ms <= p.timestamp_ms < end_ms]
    close_from = max(start_ms, end_ms - math.ceil(policy.max_holding_s * 1000))
    last_books: dict[str, Book] | None = None
    failure: str | None = None
    try:
        assert session.entry_filter is not None
        for point in data.points:
            if point.timestamp_ms >= start_ms:
                break
            if point.timestamp_ms < warmup_start:
                continue
            if fresh(point.books, policy, point.timestamp_ms):
                session.entry_filter.observe(point.books, point.timestamp_ms)
            else:
                session.entry_filter.reset()
            warmup += 1
        for point in selected:
            last_books = point.books
            session.tick(
                point.books,
                rules,
                point.timestamp_ms,
                force_exit_reason="segment_end" if point.timestamp_ms >= close_from else None,
            )
            processed += 1
            if progress is not None and processed % 1000 == 0:
                progress(
                    {
                        "processed": processed,
                        "total": len(selected),
                        "timestamp_ms": point.timestamp_ms,
                    }
                )
        # This is a causal terminal decision, using only previously seen quotes.
        # PaperSession refuses a fill if they have aged beyond its freshness gate.
        session.tick(last_books, rules, end_ms - 1, force_exit_reason="segment_end")
        if progress is not None:
            progress({"processed": processed, "total": len(selected), "timestamp_ms": end_ms - 1})
    except Exception as exc:
        failure = type(exc).__name__
        raise
    finally:
        try:
            summary = session.summary()
            segment_status = (
                "FAILED" if failure else "UNRESOLVED" if summary["cycle"] else "COMPLETED"
            )
            summary.update(
                segment_status=segment_status,
                start_ms=start_ms,
                end_ms=end_ms,
                processed=processed,
                warmup_points=warmup,
                funding_status="UNVERIFIED",
            )
            session.export(output)
            write_json(output / "summary.json", summary)
            write_json(
                output / "segment.json",
                {
                    "mode": "offline_received_book_replay",
                    "execution_enabled": False,
                    "symbol": symbol,
                    "archive_sha256": audit["archive_sha256"],
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "entry_cutoff_ms": close_from,
                    "segment_status": segment_status,
                    "processed": processed,
                    "warmup_points": warmup,
                    "initial_balance_per_venue": "1000",
                    "policy": asdict(policy),
                    "entry_filter": asdict(entry_filter),
                    "history_gate": history_gate.metadata() if history_gate else None,
                    "depth_sizing": asdict(depth_sizing) if depth_sizing else None,
                    "legacy_unfiltered": history_gate is None,
                    "rules": {v: asdict(r) for v, r in rules.items()},
                    "funding_status": "UNVERIFIED",
                    "closed": summary["cycle"] is None,
                    "closed_pair_net_before_funding": summary["closed_pair_net_before_funding"],
                    "cash_change_before_funding": summary["cash_change_before_funding"],
                    "error": failure,
                },
            )
        finally:
            session.close()
    return summary
