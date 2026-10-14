"""Bounded public order-flow collection for a separately identified instrument.

This entry point records data and optionally evaluates a new model. It has no
account credentials, execution client, order ledger, or pretrained-model input.
"""

from __future__ import annotations

import argparse
import math
import os
import signal
import threading
import time
from pathlib import Path
from typing import Any

from .live import RawRecorder, write_json

VENUES = ("bybit", "bitget")


def _stream_failure(stats: dict[str, Any], *, check_continuity: bool = True) -> str | None:
    if stats.get("fatal_error"):
        return "stream_fatal"
    if any(stats.get(key, 0) for key in ("dropped_events", "dropped_updates", "purged_events")):
        return "stream_queue_loss"
    if check_continuity and any(
        stats.get(venue, {}).get(key, 0)
        for venue in VENUES
        for key in ("disconnects", "invalidations")
    ):
        return "stream_discontinuity"
    return None


def _report(output: Path, status: dict[str, Any]) -> None:
    lines = [
        "# 公开订单流采集",
        "",
        f"- 合约：{status['symbol']}；交易所：Bybit / Bitget。",
        f"- 状态：{status['status']}；实际采集时长：{status['elapsed_seconds']:.3f} 秒。",
        f"- 原始消息：{status['raw_frames']}；已消费有效事件：{status['events']}。",
        "- `public-ws.jsonl.gz` 保留交易所事件时间、本机接收时间及完整原始消息。",
        "- `status.json` 保留逐所盘口、成交、断线、队列丢弃及错误统计。",
        "- 此轮仅采集订单流并按需训练独立模型，无模拟或真实订单，也不产生交易损益。",
        "- 公开盘口为按价位聚合的 L2 增量，并非每一张委托的 MBO 数据。",
    ]
    if status.get("evaluation_status"):
        lines.append(f"- 独立回放评估：{status['evaluation_status']}；结果在 `evaluation/`。")
    if status.get("error") or status.get("evaluation_error"):
        lines.append(f"- 错误：{status.get('error') or status.get('evaluation_error')}。")
    (output / "report.md").write_text("\n".join(lines) + "\n")


def run(
    symbol: str,
    output: Path,
    seconds: float,
    proxy: str | None = "http://127.0.0.1:7897",
    *,
    evaluate: bool = False,
) -> int:
    from .stream import StreamBooks, validate_symbol

    symbol = validate_symbol(symbol)
    if not math.isfinite(seconds) or not 0 < seconds <= 1800:
        raise ValueError("collection duration must be in (0, 1800] seconds")
    # Never overwrite an existing recording, including an incomplete one.
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    started_ms, started = int(time.time() * 1000), time.monotonic()
    events = {venue: {"book": 0, "trade": 0, "invalidate": 0} for venue in VENUES}
    book_message_types = {
        "bybit": {"snapshot": 0, "delta": 0},
        "bitget": {"snapshot": 0, "update": 0},
    }
    metadata = {
        "mode": "order_flow_collection",
        "symbol": symbol,
        "venues": list(VENUES),
        "source": {"bybit": "publicTrade + orderbook.50", "bitget": "publicTrade + books"},
        "book_channels": {"bybit": "orderbook.50", "bitget": "books"},
        "pid": os.getpid(),
        "started_ms": started_ms,
        "requested_seconds": seconds,
        "execution_enabled": False,
        "dataset": "Independent single-symbol recording; no pretrained model is loaded.",
        "evaluate_after_collection": evaluate,
    }
    write_json(output / "run_metadata.json", metadata)
    status: dict[str, Any] = {
        **metadata,
        "status": "RUNNING",
        "heartbeat_ms": started_ms,
        "elapsed_seconds": 0.0,
        "raw_frames": 0,
        "events": events,
        "book_message_types": book_message_types,
        "stream": {},
    }
    write_json(output / "status.json", status)
    stop = threading.Event()
    handlers: dict[int, Any] = {}
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGINT, signal.SIGTERM):
            handlers[sig] = signal.signal(sig, lambda *_: stop.set())
    recorder: RawRecorder | None = None
    feed: StreamBooks | None = None
    failure: str | None = None
    stopped = False
    try:
        try:
            with (
                RawRecorder(output / "public-ws.jsonl.gz") as recorder,
                StreamBooks(
                    proxy=proxy,
                    symbol=symbol,
                    on_message=recorder,
                    event_mode=True,
                    bitget_depth_channel="books",
                ) as feed,
            ):
                deadline, next_status = started + seconds, started + 2
                while time.monotonic() < deadline and not stop.is_set():
                    remaining = max(0.0, deadline - time.monotonic())
                    event = feed.next_event(timeout=min(0.25, remaining))
                    if event is not None:
                        venue = event["venue"]
                        events[venue][event["kind"]] += 1
                        if event["kind"] == "book" and isinstance(event.get("payload"), dict):
                            message_type = event["payload"].get(
                                "type" if venue == "bybit" else "action"
                            )
                            if (
                                isinstance(message_type, str)
                                and message_type in book_message_types[venue]
                            ):
                                book_message_types[venue][message_type] += 1
                        if event["kind"] == "invalidate":
                            failure = "stream_discontinuity"
                    status["stream"] = feed.stats()
                    failure = failure or _stream_failure(status["stream"])
                    if failure:
                        break
                    now = time.monotonic()
                    if now >= next_status:
                        status.update(
                            heartbeat_ms=int(time.time() * 1000),
                            elapsed_seconds=now - started,
                            raw_frames=recorder.count,
                        )
                        write_json(output / "status.json", status)
                        next_status = now + 2
                # Closing sockets invalidates them too. Preserve the collection's
                # continuity evidence before shutdown instead of treating closure
                # counters as a break in the recorded market-data interval.
                status["stream_before_close"] = feed.stats()
                status["collection_elapsed_seconds"] = time.monotonic() - started
                failure = failure or _stream_failure(status["stream_before_close"])
            stopped = stop.is_set()
        except KeyboardInterrupt:
            stopped, failure = True, "KeyboardInterrupt"
        except Exception as exc:
            # Exception text can contain an untrusted server response or proxy details.
            failure = type(exc).__name__
        finally:
            if feed is not None:
                try:
                    status["stream"] = feed.stats()
                    failure = failure or _stream_failure(status["stream"], check_continuity=False)
                except Exception as exc:
                    failure = failure or type(exc).__name__
            status.update(
                ended_ms=int(time.time() * 1000),
                heartbeat_ms=int(time.time() * 1000),
                elapsed_seconds=time.monotonic() - started,
                raw_frames=recorder.count if recorder is not None else 0,
                error=failure,
            )
        paired = all(events[v]["book"] > 0 and events[v]["trade"] > 0 for v in VENUES)
        incremental = (
            book_message_types["bybit"]["delta"] > 0 and book_message_types["bitget"]["update"] > 0
        )
        if stopped:
            status["status"] = "STOPPED"
        elif failure:
            status["status"] = "FAILED"
        elif not paired:
            status["status"] = "NO_PAIRED_DATA"
        elif not incremental:
            status["status"] = "NO_INCREMENTAL_DATA"
        else:
            status["status"] = "COMPLETED"
        status["collection_status"] = status["status"]
        write_json(output / "status.json", status)
        if status["status"] == "COMPLETED" and evaluate:
            from .forecast import replay

            status.update(status="EVALUATING", evaluation_status="RUNNING")
            write_json(output / "status.json", status)
            try:
                replay(output / "public-ws.jsonl.gz", output / "evaluation", symbol=symbol)
                evaluation_status = "STOPPED" if stop.is_set() else "COMPLETED"
                status.update(status=evaluation_status, evaluation_status=evaluation_status)
            except KeyboardInterrupt:
                status.update(status="STOPPED", evaluation_status="STOPPED")
            except Exception as exc:
                status.update(
                    status="EVALUATION_FAILED",
                    evaluation_status="FAILED",
                    evaluation_error=type(exc).__name__,
                )
            status["heartbeat_ms"] = int(time.time() * 1000)
            status["evaluation_ended_ms"] = status["heartbeat_ms"]
            write_json(output / "status.json", status)
        _report(output, status)
        return 0 if status["status"] == "COMPLETED" else 130 if status["status"] == "STOPPED" else 1
    finally:
        for saved_signal, handler in handlers.items():
            signal.signal(saved_signal, handler)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--symbol", required=True, help="Shared USDT perpetual symbol, e.g. ALTUSDT"
    )
    parser.add_argument("--output", required=True, type=Path, help="New recording directory")
    parser.add_argument("--seconds", type=float, default=1800)
    parser.add_argument("--proxy", default="http://127.0.0.1:7897")
    parser.add_argument(
        "--evaluate", action="store_true", help="Train/evaluate only this recording"
    )
    args = parser.parse_args()
    return run(args.symbol, args.output, args.seconds, args.proxy, evaluate=args.evaluate)


if __name__ == "__main__":
    raise SystemExit(main())
