"""Bounded, public-WebSocket ideal-fill simulation; never sends exchange orders."""

from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import signal
import sqlite3
import threading
import time
from collections.abc import Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import TracebackType
from typing import Any

from .engine import Policy
from .entry_filters import EntryFilterConfig

DEFAULT_ENTRY_FILTER = EntryFilterConfig()


def write_json(path: Path, value: dict[str, Any]) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")
    os.replace(temp, path)


class RawRecorder:
    """Thread-safe append-only gzip journal of every received public frame."""

    def __init__(self, path: Path):
        self._file = gzip.open(path, "xt", encoding="utf-8", compresslevel=3)  # noqa: SIM115
        self._lock = threading.Lock()
        self._flush_at = time.monotonic()
        self.count = 0

    def __call__(self, venue: str, received_at_ms: int, payload: dict[str, Any]) -> None:
        row = json.dumps(
            {
                "venue": venue,
                "received_at_ms": received_at_ms,
                "recorded_monotonic_ns": time.monotonic_ns(),
                "payload": payload,
            },
            separators=(",", ":"),
            allow_nan=False,
        )
        with self._lock:
            self._file.write(row + "\n")
            self.count += 1
            if time.monotonic() - self._flush_at >= 1:
                self._file.flush()
                self._flush_at = time.monotonic()

    def close(self) -> None:
        with self._lock:
            self._file.close()

    def __enter__(self) -> RawRecorder:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def funding_assessment(
    cycles: Sequence[dict[str, Any]],
    histories: Sequence[dict[str, Any]],
    active_cycle: dict[str, Any] | None,
) -> dict[str, Any]:
    if active_cycle is not None:
        return {"status": "UNRESOLVED", "funding_usdt": None, "model": "settlement_avoidance"}
    for cycle in cycles:
        entered, exited = cycle["entry_timestamp_ms"], cycle["exit_timestamp_ms"]
        for venue in ("bybit", "bitget"):
            crossed = any(
                venue in history
                and history[venue].get("error") is None
                and history[venue]["fetched_ms"] <= exited
                and entered <= history[venue]["next_ms"] <= exited
                for history in histories
            )
            if crossed:
                return {"status": "UNKNOWN", "funding_usdt": None, "model": "settlement_avoidance"}
            valid = [
                history[venue]
                for history in histories
                if venue in history
                and history[venue].get("error") is None
                and 0 <= entered - history[venue]["fetched_ms"] <= 75000
            ]
            if not valid or min(s["next_ms"] for s in valid) <= exited:
                return {"status": "UNKNOWN", "funding_usdt": None, "model": "settlement_avoidance"}
    return {
        "status": "VERIFIED_NO_SETTLEMENT",
        "funding_usdt": "0",
        "model": "settlement_avoidance",
    }


def _utc(milliseconds: int) -> str:
    return datetime.fromtimestamp(milliseconds / 1000, UTC).isoformat()


def _run_clock_reason(
    started_ms: int, started_mono: float, seconds: float, now_ms: int, now_mono: float
) -> str | None:
    """Bound a run by both clocks and fail closed after suspension or a clock jump."""
    wall_elapsed = (now_ms - started_ms) / 1000
    mono_elapsed = now_mono - started_mono
    if abs(wall_elapsed - mono_elapsed) > 5:
        return "clock_discontinuity"
    if wall_elapsed >= seconds or mono_elapsed >= seconds:
        return "session_end"
    return None


def _cycles(path: Path) -> list[dict[str, Any]]:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        return [json.loads(row[0]) for row in db.execute("SELECT value FROM cycles ORDER BY id")]


def _report(output: Path, status: dict[str, Any]) -> None:
    ledger = status.get("ledger", {})
    funding = status.get("funding", {})
    policy = status.get("policy", ledger.get("policy", {}))
    lines = [
        "# Bybit / Bitget 实时仿真",
        "",
        f"- 状态：{status['status']}",
        f"- 开始：{status.get('started_utc', '未开始')}",
        f"- 结束：{status.get('ended_utc', '运行中')}",
        f"- 运行异常：{status.get('error') or '无'}。",
        f"- 账户：两所各 1,000 USDT；{status.get('symbol', 'BTCUSDT')} 线性永续；"
        "单腿最多 250 USDT；1 倍杠杆。",
        "- 信号：按增量重建的当前可见盘口直接吃单，不使用盘口预测模型。",
        "- 成交假设：信号时的公开盘口 VWAP 全额成交，未建模排队、撮合拒单与下单网络延迟。",
        f"- 每笔成交手续费：Bybit {policy.get('bybit_fee_bps', '未知')} bp，"
        f"Bitget {policy.get('bitget_fee_bps', '未知')} bp；"
        "信号另预留退出价差和资金费缓冲。",
        "- 入场筛选参数：" + json.dumps(status.get("entry_filter"), ensure_ascii=False,
                                        default=str) + "。",
        f"- 信号评估：{ledger.get('signal_evaluations', 0)}；"
        f"达到门槛：{ledger.get('eligible_signals', 0)}。",
        f"- 模拟订单：{ledger.get('orders', 0)}；"
        f"完成双腿组合：{ledger.get('completed_cycles', 0)}。",
        "- 已平组合损益（手续费后、资金费前）："
        f"{ledger.get('closed_pair_net_before_funding', '未知')} USDT。",
        f"- 资金费验证：{funding.get('status', '待验证')}；"
        f"金额：{funding.get('funding_usdt', '未知')}。",
        f"- 账户现金：{json.dumps(ledger.get('cash', {}), ensure_ascii=False)}。",
        f"- 未闭合组合：{'有，需查看账本' if ledger.get('cycle') else '无'}。",
        "",
        "## 记录",
        "",
        "- `signals.csv`：每次盘口更新的两个方向评估、成本、触发与阻断原因。",
        "- `orders.csv`：每条模拟订单及成交数量、价格、费用、组合 ID。",
        "- `cycles.csv`：每组多空腿的进出场、逐腿与组合损益。",
        "- `equity.csv`：账户现金、保证金与未实现损益快照。",
        "- `ledger.sqlite`：完整追加账本；运行时也可只读查询。",
        "- `public-ws.jsonl.gz`：收到的全部公开盘口和逐笔成交消息，"
        "含交易所时间及本机接收时间；批量成交不拆丢。",
        "- `funding-schedules.jsonl`：结算时刻、费率和标记价格的公开查询证据。",
        "- `status.json`：执行状态、订阅计数、错误与资金费验证。",
        "",
        "本报告只描述本次收到的数据。低于成本门槛时不制造订单；空交易表保留字段。",
        "信号按本机收到的数据顺序处理；过期或时间偏差过大的报价禁止成交。",
        "逐笔流的断线间隙不补成连续数据，实际断线和队列溢出见 status.json。",
    ]
    (output / "report.md").write_text("\n".join(lines) + "\n")


def run(
    output: Path, seconds: float, proxy: str | None, *, symbol: str = "BTCUSDT",
    fee_bps: Decimal = Decimal("3"),
    entry_filter: EntryFilterConfig | None = DEFAULT_ENTRY_FILTER,
) -> int:
    # Imports here keep pure recording/report helpers usable without network setup.
    from .funding import FundingGuard
    from .market import MarketData
    from .paper import PaperSession
    from .stream import StreamBooks, validate_symbol

    symbol = validate_symbol(symbol)
    if not math.isfinite(seconds) or not 0 < seconds <= 86400:
        raise ValueError("seconds must be finite and in (0,86400]")
    if (
        not isinstance(fee_bps, Decimal) or not fee_bps.is_finite()
        or not Decimal(0) <= fee_bps <= Decimal(100)
    ):
        raise ValueError("fee_bps must be a finite Decimal in [0,100]")
    if entry_filter is not None and not isinstance(entry_filter, EntryFilterConfig):
        raise ValueError("entry_filter must be EntryFilterConfig or None")
    policy = Policy(max_cycles=10000, bybit_fee_bps=fee_bps, bitget_fee_bps=fee_bps)
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    started_ms: int | None = None
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    metadata = {
        "mode": "ideal_signal_fill",
        "symbol": symbol,
        "pair": f"{symbol[:-4]}/USDT:USDT",
        "signal_model": "confirmed_depth_taker",
        "model_used": False,
        "execution_enabled": False,
        "book_channels": {"bybit": "orderbook.50", "bitget": "books"},
        "pid": os.getpid(),
        "requested_seconds": seconds,
        "timing_guard": {"deadlines": "wall_and_monotonic", "max_clock_drift_seconds": 5},
        "initial_usdt_per_venue": "1000",
        "policy": asdict(policy),
        "entry_filter": asdict(entry_filter) if entry_filter is not None else None,
        "created_utc": _utc(int(time.time() * 1000)),
        "execution": "Depth VWAP, full simultaneous fills assumed; no live orders.",
        "signal_evaluation": "Every received, validated book update; expired queued data rejected.",
        "source": {"bybit": "publicTrade + orderbook.50", "bitget": "publicTrade + books"},
    }
    write_json(output / "run_metadata.json", metadata)
    status: dict[str, Any] = {**metadata, "status": "STARTING"}
    write_json(output / "status.json", status)
    session = PaperSession(
        output / "ledger.sqlite", policy, Decimal("1000"), symbol=symbol, entry_filter=entry_filter
    )
    histories: list[dict[str, Any]] = []
    last_stream_stats: dict[str, Any] = {}
    failure: str | None = None
    future: Future[dict[str, Any]] | None = None
    recorder: RawRecorder | None = None
    feed: StreamBooks | None = None
    try:
        with (
            MarketData(proxy=proxy, symbol=symbol) as market,
            FundingGuard(proxy=proxy, symbol=symbol) as guard,
        ):
            rules = market.fetch_rules()
            write_json(output / "instruments.json", {v: asdict(r) for v, r in rules.items()})
            with (output / "funding-schedules.jsonl").open("x") as schedules:

                def save_schedule(snapshot: dict[str, Any]) -> None:
                    histories.append(snapshot)
                    schedules.write(json.dumps(snapshot, separators=(",", ":")) + "\n")
                    schedules.flush()

                save_schedule(guard.refresh(int(time.time() * 1000)))
                with (
                    RawRecorder(output / "public-ws.jsonl.gz") as recorder,
                    StreamBooks(
                        proxy=proxy, symbol=symbol, bitget_depth_channel="books",
                        on_message=recorder,
                    ) as feed,
                    ThreadPoolExecutor(max_workers=1) as pool,
                ):
                    warmup_deadline = time.monotonic() + 45
                    warmup_wall_deadline = time.time() + 45
                    while not stop.is_set() and feed.snapshot() is None:
                        if (
                            time.monotonic() >= warmup_deadline
                            or time.time() >= warmup_wall_deadline
                            or feed.stats().get("fatal_error")
                        ):
                            raise RuntimeError("stream_startup_failed")
                        stop.wait(0.1)
                    if stop.is_set():
                        raise RuntimeError("stopped_before_start")
                    started_ms = int(time.time() * 1000)
                    start = time.monotonic()
                    next_refresh, next_status = start + 30, start
                    status.update(
                        status="RUNNING",
                        started_utc=_utc(started_ms),
                        planned_end_utc=_utc(started_ms + int(seconds * 1000)),
                    )
                    invalidations: tuple[int, ...] = (0, 0)
                    while not stop.is_set():
                        now_mono = time.monotonic()
                        clock_reason = _run_clock_reason(
                            started_ms, start, seconds, int(time.time() * 1000), now_mono
                        )
                        if clock_reason:
                            if clock_reason == "clock_discontinuity":
                                failure = clock_reason
                            break
                        if future is not None and future.done():
                            save_schedule(future.result())
                            future = None
                        if now_mono >= next_refresh and future is None:
                            future = pool.submit(guard.refresh, int(time.time() * 1000))
                            next_refresh = now_mono + 30
                        books = feed.next_update(timeout=0.25)
                        now_ms = int(time.time() * 1000)
                        # Waiting for a quote may cross the deadline or a machine suspension.
                        clock_reason = _run_clock_reason(
                            started_ms, start, seconds, now_ms, time.monotonic()
                        )
                        if clock_reason or stop.is_set():
                            if clock_reason == "clock_discontinuity":
                                failure = clock_reason
                            break
                        last_stream_stats = feed.stats()
                        current_invalidations = tuple(
                            last_stream_stats.get(v, {}).get("invalidations", 0)
                            for v in ("bybit", "bitget")
                        )
                        if current_invalidations != invalidations:
                            session.reset_entry_filter()
                            invalidations = current_invalidations
                        if last_stream_stats.get("fatal_error"):
                            failure = str(last_stream_stats["fatal_error"])
                            break
                        funding_reason = guard.decision(now_ms, policy.max_holding_s)
                        session.tick(
                            books,
                            rules,
                            now_ms,
                            force_exit_reason=f"funding_guard:{funding_reason}"
                            if funding_reason
                            else None,
                        )
                        if now_mono >= next_status:
                            status.update(
                                ledger=session.summary(),
                                stream=last_stream_stats,
                                raw_frames=recorder.count,
                                heartbeat_utc=_utc(now_ms),
                                funding_guard=funding_reason,
                            )
                            write_json(output / "status.json", status)
                            next_status = now_mono + 10
                    # Do not manufacture an end-of-run fill from an expired cached book.
                    drain_started_ms = int(time.time() * 1000)
                    drain_started_mono = time.monotonic()
                    while True:
                        books = feed.snapshot()
                        now_ms = int(time.time() * 1000)
                        drain_reason = _run_clock_reason(
                            drain_started_ms, drain_started_mono, 30, now_ms, time.monotonic()
                        )
                        if drain_reason:
                            if drain_reason == "clock_discontinuity":
                                failure = failure or drain_reason
                            break
                        last_tick_ms = session.summary().get("last_tick_ms")
                        if last_tick_ms is not None and now_ms < last_tick_ms:
                            # Never invent timestamps to close a position after a backward jump.
                            failure = failure or "clock_discontinuity"
                            break
                        session.tick(
                            books, rules, now_ms, force_exit_reason=failure or "session_end"
                        )
                        if session.summary().get("cycle") is None:
                            break
                        stop.wait(0.1) if not stop.is_set() else time.sleep(0.1)
                    if future is not None:
                        save_schedule(future.result())
                    last_stream_stats = feed.stats()
                    status["stream_before_close"] = last_stream_stats
                    status["raw_frames"] = recorder.count
    except Exception as exc:
        # No remote payloads, environment variables or credentials in diagnostics.
        failure = failure or (
            type(exc).__name__ + ":" + str(exc)
            if str(exc) in {"stream_startup_failed", "stopped_before_start"}
            else type(exc).__name__
        )
    finally:
        # Capture counters after the receiver threads have closed the recorder window.
        # The before-close snapshot distinguishes shutdown invalidations from data gaps.
        if feed is not None:
            last_stream_stats = feed.stats()
        if recorder is not None:
            status["raw_frames"] = recorder.count
        status.update(
            ledger=session.summary(),
            stream=last_stream_stats,
            ended_utc=_utc(int(time.time() * 1000)),
            error=failure,
        )
        status["funding"] = funding_assessment(
            _cycles(output / "ledger.sqlite"), histories, status["ledger"].get("cycle")
        )
        status["status"] = (
            "FAILED"
            if failure
            else "UNRESOLVED"
            if status["ledger"].get("cycle")
            else "STOPPED_EARLY"
            if stop.is_set()
            else "COMPLETED"
        )
        if started_ms is not None:
            status["started_utc"] = _utc(started_ms)
            status["elapsed_seconds"] = (int(time.time() * 1000) - started_ms) / 1000
        session.export(output)
        session.close()
        write_json(output / "status.json", status)
        _report(output, status)
    print(
        json.dumps(
            {
                "output": str(output),
                "status": status["status"],
                "completed_cycles": status["ledger"].get("completed_cycles"),
                "net_before_funding": status["ledger"].get("closed_pair_net_before_funding"),
            }
        ),
        flush=True,
    )
    return 0 if status["status"] == "COMPLETED" else 2


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seconds", type=float, default=1800)
    parser.add_argument("--proxy", default="http://127.0.0.1:7897")
    parser.add_argument("--fee-bps", type=Decimal, default=Decimal("3"),
                        help="Per-fill fee at each venue, in basis points (default: 3 = 0.03%%)")
    defaults = EntryFilterConfig()
    parser.add_argument("--volatility-window-s", type=int, default=defaults.volatility_window_s)
    parser.add_argument("--volatility-sample-ms", type=int, default=defaults.volatility_sample_ms)
    parser.add_argument("--min-volatility-bps", type=Decimal, default=defaults.min_volatility_bps,
                        help="Minimum realized volatility per venue; 0 disables volatility gate")
    parser.add_argument("--liquidity-band-bps", type=Decimal, default=defaults.liquidity_band_bps)
    parser.add_argument("--min-depth-usdt", type=Decimal, default=defaults.min_depth_usdt,
                        help="Minimum executable-side depth at each venue within the price band")
    args = parser.parse_args()
    try:
        entry_filter = EntryFilterConfig(
            volatility_window_s=args.volatility_window_s,
            volatility_sample_ms=args.volatility_sample_ms,
            min_volatility_bps=args.min_volatility_bps,
            liquidity_band_bps=args.liquidity_band_bps,
            min_depth_usdt=args.min_depth_usdt,
        )
    except ValueError as exc:
        parser.error(str(exc))
    if not math.isfinite(args.seconds) or not 0 < args.seconds <= 86400:
        parser.error("seconds must be finite and in (0,86400]")
    raise SystemExit(
        run(args.output.resolve(), args.seconds, args.proxy or None,
            symbol=args.symbol, fee_bps=args.fee_bps, entry_filter=entry_filter)
    )


if __name__ == "__main__":
    main()
