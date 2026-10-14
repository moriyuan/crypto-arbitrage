"""Receipt-causal order-flow nowcasting, chronological evaluation and shadow signals.

Forecast prices are estimates. They are never supplied to the execution ledger as fills.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt

from .live import RawRecorder, write_json
from .stream import validate_symbol

Array = npt.NDArray[np.float64]


def label_index(
    source: npt.NDArray[Any], received: npt.NDArray[Any], decision_ms: int
) -> int | None:
    """Hindsight target: last published source state at decision time, not next state."""
    i = int(np.searchsorted(source, decision_ms, side="right")) - 1
    if i < 0 or i >= len(source) - 1 or decision_ms - int(source[i]) > 25:
        return None
    if int(received[i]) <= decision_ms:
        return None
    return i


def segment_indices(
    decisions: npt.NDArray[Any],
    revealed: npt.NDArray[Any],
    start: int,
    end: int,
    embargo_ms: int = 2000,
) -> tuple[npt.NDArray[Any], npt.NDArray[Any], npt.NDArray[Any]]:
    first, second = start + int((end - start) * 0.6), start + int((end - start) * 0.8)
    return (
        np.flatnonzero((decisions < first - embargo_ms) & (revealed <= first)),
        np.flatnonzero(
            (decisions >= first + embargo_ms)
            & (decisions < second - embargo_ms)
            & (revealed <= second)
        ),
        np.flatnonzero(
            (decisions >= second + embargo_ms) & (decisions < end - embargo_ms) & (revealed <= end)
        ),
    )


@dataclass
class LinearNowcast:
    names: list[str]
    mean: Array
    scale: Array
    coef: Array
    intercept: float

    @classmethod
    def fit(cls, x: Array, y: Array, names: list[str]) -> LinearNowcast:
        if (
            x.ndim != 2
            or len(x) < 10
            or x.shape[1] != len(names)
            or y.shape != (len(x),)
            or not np.isfinite(x).all()
            or not np.isfinite(y).all()
        ):
            raise ValueError("invalid training data")
        mean, scale = x.mean(axis=0), x.std(axis=0)
        scale[scale < 1e-9] = 1
        z = np.clip((x - mean) / scale, -8, 8)
        intercept = float(y.mean())
        coef = np.linalg.solve(z.T @ z + 10 * np.eye(x.shape[1]), z.T @ (y - intercept))
        return cls(names, mean, scale, coef, intercept)

    def predict(self, x: Array) -> Array:
        if x.ndim != 2 or x.shape[1] != len(self.names) or not np.isfinite(x).all():
            raise ValueError("invalid prediction features")
        return np.asarray(
            np.clip(
                np.clip((x - self.mean) / self.scale, -8, 8) @ self.coef + self.intercept, -20, 20
            )
        )

    def in_domain(self, x: Array) -> bool:
        return bool(np.isfinite(x).all() and (abs((x - self.mean) / self.scale) <= 8).all())

    def to_json(self) -> dict[str, Any]:
        return {
            "names": self.names,
            "mean": self.mean.tolist(),
            "scale": self.scale.tolist(),
            "coef": self.coef.tolist(),
            "intercept": self.intercept,
        }

    @classmethod
    def from_json(cls, row: dict[str, Any]) -> LinearNowcast:
        names = row["names"]
        if (
            not isinstance(names, list)
            or not names
            or len(names) > 100
            or not all(isinstance(s, str) for s in names)
        ):
            raise ValueError("invalid model features")
        vectors = [np.asarray(row[k], dtype=float) for k in ("mean", "scale", "coef")]
        if any(v.shape != (len(names),) or not np.isfinite(v).all() for v in vectors):
            raise ValueError("invalid model vectors")
        intercept = float(row["intercept"])
        if not math.isfinite(intercept) or (vectors[1] <= 0).any():
            raise ValueError("invalid model scale")
        return cls(names, vectors[0], vectors[1], vectors[2], intercept)


def metrics(error: Array) -> dict[str, float]:
    return {
        "mae_bps": float(np.mean(abs(error))),
        "p95_absolute_error_bps": float(np.quantile(abs(error), 0.95)),
        "rmse_bps": float(np.sqrt(np.mean(error**2))),
        "bias_bps": float(np.mean(error)),
    }


def check_frame_symbol(venue: str, payload: dict[str, Any], symbol: str) -> None:
    """Reject mixed-asset recordings before normal parse-error recovery can hide them."""
    identities: list[object] = []
    if venue == "bybit":
        topic = payload.get("topic")
        if isinstance(topic, str):
            identities.append(topic.rsplit(".", 1)[-1])
    elif venue == "bitget":
        arg = payload.get("arg")
        if isinstance(arg, dict) and "symbol" in arg:
            identities.append(arg["symbol"])
    else:
        raise ValueError("unexpected archive venue")
    data = payload.get("data")
    rows = data if isinstance(data, list) else [data]
    for row in rows:
        if isinstance(row, dict):
            identities.extend(row[k] for k in ("s", "symbol") if k in row)
    if any(value != symbol for value in identities):
        raise ValueError("archive symbol mismatch")


def replay(
    archive: Path, output: Path, sample_ms: int = 50, *, symbol: str = "BTCUSDT"
) -> None:
    from .nowcast import FlowState

    symbol = validate_symbol(symbol)
    if type(sample_ms) is not int or not 1 <= sample_ms <= 1000:
        raise ValueError("invalid model sampling cadence")
    output.mkdir(parents=True, exist_ok=False)
    states = {v: FlowState(v, symbol=symbol) for v in ("bybit", "bitget")}
    histories: dict[str, list[dict[str, Any]]] = {v: [] for v in states}
    samples: dict[str, list[dict[str, Any]]] = {v: [] for v in states}
    previous: dict[str, Any] = {}
    last_sample = dict.fromkeys(states, 0)
    names: list[str] = []
    now = 0
    errors: dict[str, int] = {}
    channels: dict[str, set[str]] = {v: set() for v in states}
    frames = 0
    with gzip.open(archive, "rt") as stream:
        for line in stream:
            frame = json.loads(line)
            frames += 1
            venue, received, payload = (
                frame["venue"],
                int(frame["received_at_ms"]),
                frame["payload"],
            )
            check_frame_symbol(venue, payload, symbol)
            # Preserve physical recorder order. Equal-clock cross-thread frames never go backwards.
            now = max(now, received)
            try:
                changed = states[venue].consume(payload, received)
            except ValueError as exc:
                states[venue].reset()
                errors[type(exc).__name__] = errors.get(type(exc).__name__, 0) + 1
                continue
            if not changed:
                continue
            topic = payload.get("topic", payload.get("arg", {}).get("topic", ""))
            if topic.startswith("orderbook."):
                channels[venue].add(topic.rsplit(".", 1)[0])
            elif topic.startswith("books"):
                channels[venue].add(topic)
            features = states[venue].features(now)
            if features is None:
                continue
            book = features["confirmed_book"]
            if previous.get(venue) is not book:
                previous[venue] = book
                histories[venue].append(
                    {
                        "source": book.exchange_ts_ms,
                        "received": received,
                        "bid": float(book.bids[0][0]),
                        "ask": float(book.asks[0][0]),
                    }
                )
            if now - last_sample[venue] < sample_ms:
                continue
            numeric = features["numeric"]
            if not names:
                names = sorted(numeric)
            if sorted(numeric) != names:
                raise ValueError("unstable feature schema")
            if not 0 <= now - book.exchange_ts_ms <= 2000:
                continue
            samples[venue].append(
                {
                    "decision_ms": now,
                    "x": [float(numeric[n]) for n in names],
                    "mid": features["midpoint"],
                    "bid": features["bestbid"],
                    "ask": features["bestask"],
                    "micro_offset": numeric["microprice_offset_bps"],
                    "projected_offset": numeric["projected_mid_offset_bps"],
                }
            )
            last_sample[venue] = now
    if channels["bybit"] != {"orderbook.50"} or len(channels["bitget"]) != 1:
        raise ValueError("missing or mixed archive depth channels")
    book_channels = {v: next(iter(values)) for v, values in channels.items()}
    model_bundle: dict[str, Any] = {
        "version": 2,
        "book_channels": book_channels,
        "symbol": symbol,
        "horizon_ms": 0,
        "sample_ms": sample_ms,
        "venues": {},
        "label": "Last received-later source book at decision time; "
        "<=25ms source age; synchronized clocks assumed",
        "execution_enabled": False,
    }
    result: dict[str, Any] = {
        "archive": str(archive),
        "book_channels": book_channels,
        "symbol": symbol,
        "frames": frames,
        "parse_resets": errors,
        "venues": {},
        "limitation": (
            "Single-symbol recording; sampled L2 labels, not matching-engine truth. Shadow only."
        ),
    }
    with gzip.open(output / "holdout-predictions.csv.gz", "wt", newline="") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=[
                "symbol",
                "venue",
                "partition",
                "decision_ms",
                "label_source_ms",
                "label_available_ms",
                "observed_bid",
                "observed_ask",
                "actual_bid",
                "actual_ask",
                "predicted_bid",
                "predicted_ask",
                "midpoint_error_bps",
                "bbo_max_error_bps",
                "q95_error_bps",
            ],
        )
        writer.writeheader()
        for venue in states:
            history = histories[venue]
            src = np.array([h["source"] for h in history], dtype=np.int64)
            recv = np.array([h["received"] for h in history], dtype=np.int64)
            if (np.diff(src) < 0).any():
                raise ValueError("source timeline reset; split archive by connection")
            rows = []
            for sample in samples[venue]:
                index = label_index(src, recv, sample["decision_ms"])
                if index is None:
                    continue
                sample.update(
                    target=history[index], revealed=max(int(recv[index]), int(recv[index + 1]))
                )
                rows.append(sample)
            if len(rows) < 1000:
                raise ValueError("insufficient causal labels")
            x = np.array([r["x"] for r in rows])
            mid = np.array([r["mid"] for r in rows])
            observed_bid = np.array([r["bid"] for r in rows])
            observed_ask = np.array([r["ask"] for r in rows])
            bid = np.array([r["target"]["bid"] for r in rows])
            ask = np.array([r["target"]["ask"] for r in rows])
            y = ((bid + ask) / 2 - mid) / mid * 10000
            decisions = np.array([r["decision_ms"] for r in rows])
            revealed = np.array([r["revealed"] for r in rows])
            train, cal, test = segment_indices(
                decisions,
                revealed,
                samples[venue][0]["decision_ms"],
                samples[venue][-1]["decision_ms"],
            )
            if min(len(train), len(cal), len(test)) < 100:
                raise ValueError("insufficient chronological partitions")
            model = LinearNowcast.fit(x[train], y[train], names)
            predicted = model.predict(x)
            pbid = observed_bid + predicted * mid / 10000
            pask = observed_ask + predicted * mid / 10000
            bbo_error = np.maximum(abs(pbid - bid), abs(pask - ask)) / mid * 10000
            q95 = float(np.quantile(bbo_error[cal], 0.95))
            calibration_approved = bool(
                np.mean(abs(predicted[cal] - y[cal])) < np.mean(abs(y[cal]))
            )
            row_result: dict[str, Any] = {
                "samples": len(samples[venue]),
                "causal_labels": len(rows),
                "label_censored": len(samples[venue]) - len(rows),
                "train": len(train),
                "calibration": len(cal),
                "test": len(test),
                "calibration_approved": calibration_approved,
                "q95_bbo_error_bps": q95,
                "baseline": metrics(-y[test]),
                "order_flow": metrics(predicted[test] - y[test]),
                "microprice": metrics(np.array([r["micro_offset"] for r in rows])[test] - y[test]),
                "trade_depletion": metrics(
                    np.array([r["projected_offset"] for r in rows])[test] - y[test]
                ),
                "bbo_coverage_on_test": float(np.mean(bbo_error[test] <= q95)),
                "bbo_mae_bps": float(np.mean(bbo_error[test])),
                "source_target_age_max_ms": max(
                    int(r["decision_ms"]) - int(r["target"]["source"]) for r in rows
                ),
                "label_available_before_train_cutoff": True,
            }
            result["venues"][venue] = row_result
            model_bundle["venues"][venue] = {
                "model": model.to_json(),
                "q95_bps": q95,
                "calibration_approved": calibration_approved,
                "training_end_ms": int(revealed[train].max()),
                "calibration_end_ms": int(revealed[cal].max()),
            }
            for partition, indices in (("calibration", cal), ("test", test)):
                for i in indices:
                    r = rows[int(i)]
                    writer.writerow(
                        {
                            "symbol": symbol,
                            "venue": venue,
                            "partition": partition,
                            "decision_ms": r["decision_ms"],
                            "label_source_ms": r["target"]["source"],
                            "label_available_ms": r["revealed"],
                            "observed_bid": r["bid"],
                            "observed_ask": r["ask"],
                            "actual_bid": bid[i],
                            "actual_ask": ask[i],
                            "predicted_bid": pbid[i],
                            "predicted_ask": pask[i],
                            "midpoint_error_bps": predicted[i] - y[i],
                            "bbo_max_error_bps": bbo_error[i],
                            "q95_error_bps": q95,
                        }
                    )
    write_json(output / "model.json", model_bundle)
    write_json(output / "evaluation.json", result)
    lines = [
        f"# {symbol} 订单流盘口状态估计验收",
        "",
        "按原始接收顺序重建；训练/校准/测试按时间60%/20%/20%切分，分界隔离2秒。",
        "目标是本地决策时刻之前25毫秒内、随后才收到的最近源盘口，假设时钟同步。不是撮合引擎全量真值。",
        "模型只拟合中间价修正，保留已确认价差；成交对数量的补减仅是启发式投影，不是已确认委托。",
        "",
        "| 场所 | 测试样本 | 原盘口MAE(bp) | 订单流MAE(bp) | 订单流P95误差(bp) | 误差带覆盖率 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for v, r in result["venues"].items():
        lines.append(
            f"| {v} | {r['test']} | {r['baseline']['mae_bps']:.4f} | "
            f"{r['order_flow']['mae_bps']:.4f} | "
            f"{r['order_flow']['p95_absolute_error_bps']:.4f} | "
            f"{r['bbo_coverage_on_test']:.2%} |"
        )
    lines += [
        "",
        "盘口数量、未来撤单与逐单排队未获得真值验证。误差带来自前段校准样本，不保证新行情覆盖率。",
        "预测只进入影子信号记录，未作为模拟或真实成交；模型表现差时不会通过校准门槛。",
    ]
    (output / "report.md").write_text("\n".join(lines) + "\n")
    print(json.dumps(result, ensure_ascii=False), flush=True)


def estimate(features: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    model = LinearNowcast.from_json(row["model"])
    x = np.array([[float(features["numeric"][n]) for n in model.names]])
    shift = float(model.predict(x)[0])
    mid = float(features["midpoint"])
    delta = mid * shift / 10000
    q95 = float(row["q95_bps"])
    if not math.isfinite(q95) or q95 < 0:
        raise ValueError("invalid error bound")
    projection = features["projected_book"]
    raw_age = (
        features["numeric"]["receive_age_ms"]
        + features["book_received_ms"]
        - features["book_ts_ms"]
    )
    valid_age = 0 <= raw_age <= 2000
    return {
        "bid": features["bestbid"] + delta,
        "ask": features["bestask"] + delta,
        "midpoint": mid + delta,
        "shift_bps": shift,
        "q95_bps": q95,
        "book_ts_ms": features["book_ts_ms"],
        "book_received_ms": features["book_received_ms"],
        "source_age_ms": raw_age,
        "gate": bool(
            row["calibration_approved"]
            and valid_age
            and model.in_domain(x)
            and projection is not None
        ),
        "projected_bids": [[str(p), str(q)] for p, q in projection.bids[:5]] if projection else [],
        "projected_asks": [[str(p), str(q)] for p, q in projection.asks[:5]] if projection else [],
        "quantity_label": "Unconfirmed trade depletion; BBO prices are model estimates",
        "confirmed_bid": features["bestbid"],
        "confirmed_ask": features["bestask"],
    }


def shadow_signal(predictions: dict[str, dict[str, Any]], now_ms: int) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if set(predictions) != {"bybit", "bitget"}:
        return result
    for long, short in (("bybit", "bitget"), ("bitget", "bybit")):
        buy, sell = predictions[long], predictions[short]
        ref = (buy["ask"] + sell["bid"]) / 2
        edge = (sell["bid"] - buy["ask"]) / ref * 10000
        # Price errors are expressed relative to each venue's confirmed midpoint.
        buy_error = (buy["confirmed_bid"] + buy["confirmed_ask"]) / 2 * buy["q95_bps"] / 10000
        sell_error = (sell["confirmed_bid"] + sell["confirmed_ask"]) / 2 * sell["q95_bps"] / 10000
        conservative = (
            (sell["bid"] - sell_error - buy["ask"] - buy_error) / ref * 10000 - 24 - 2 - 2 - 2
        )
        same_time = all(0 <= now_ms - p["book_ts_ms"] <= 2000 for p in (buy, sell))
        gate = bool(buy["gate"] and sell["gate"] and same_time)
        result.append(
            {
                "decision_ms": now_ms,
                "long": long,
                "short": short,
                "predicted_edge_bps": edge,
                "conservative_net_edge_bps": conservative,
                "threshold_bps": 5,
                "eligible": gate and conservative >= 5,
                "model_gate": gate,
                "mode": "order_flow_shadow",
                "execution_enabled": False,
            }
        )
    return result


def live(
    model_path: Path, output: Path, seconds: float, *, symbol: str = "BTCUSDT"
) -> None:
    from .nowcast import FlowState
    from .stream import StreamBooks

    symbol = validate_symbol(symbol)
    if not math.isfinite(seconds) or not 0 < seconds <= 1800:
        raise ValueError("seconds must be in (0,1800]")
    bundle = json.loads(model_path.read_text())
    if bundle.get("version") != 2 or set(bundle.get("venues", {})) != {"bybit", "bitget"}:
        raise ValueError("invalid model bundle; retrain legacy models with explicit symbol")
    if bundle.get("symbol") != symbol:
        raise ValueError("model symbol mismatch")
    channels = bundle.get("book_channels", {})
    if (
        channels.get("bybit") != "orderbook.50"
        or channels.get("bitget") not in {"books50", "books"}
    ):
        raise ValueError("unsupported model depth channels; collect a matching new dataset")
    sample_ms = bundle["sample_ms"]
    if type(sample_ms) is not int or not 1 <= sample_ms <= 1000:
        raise ValueError("invalid model sampling cadence")
    output.mkdir(parents=True, exist_ok=False)
    states = {v: FlowState(v, symbol=symbol) for v in ("bybit", "bitget")}
    counts = {"events": 0, "predictions": 0, "signal_evaluations": 0, "eligible_signals": 0}
    final_stats = {}
    last_sample = 0
    status = "COMPLETED"
    error: str | None = None
    with (
        RawRecorder(output / "public-ws.jsonl.gz") as recorder,
        gzip.open(output / "predictions.jsonl.gz", "xt") as forecast_file,
        (output / "signals.jsonl").open("x") as signal_file,
        StreamBooks(
            symbol=symbol, bitget_depth_channel=channels["bitget"],
            event_mode=True, on_message=recorder,
        ) as feed,
    ):
        deadline = time.monotonic() + seconds
        try:
            while time.monotonic() < deadline:
                event = feed.next_event(timeout=0.25)
                if feed.stats().get("fatal_error"):
                    status = "STREAM_FAILED"
                    break
                if event is None:
                    continue
                counts["events"] += 1
                venue = event["venue"]
                if event["kind"] == "invalidate":
                    states[venue].reset()
                    continue
                states[venue].consume(event["payload"], event["received_at_ms"])
                now = int(time.time() * 1000)
                if now - last_sample < sample_ms:
                    continue
                predictions = {}
                for v, state in states.items():
                    features = state.features(now)
                    if features is None:
                        continue
                    predictions[v] = estimate(features, bundle["venues"][v])
                forecast_file.write(
                    json.dumps({"symbol": symbol, "decision_ms": now, "venues": predictions}) + "\n"
                )
                counts["predictions"] += len(predictions)
                for signal_row in shadow_signal(predictions, now):
                    signal_row["symbol"] = symbol
                    signal_file.write(json.dumps(signal_row) + "\n")
                    counts["signal_evaluations"] += 1
                    counts["eligible_signals"] += int(signal_row["eligible"])
                last_sample = now
        except KeyboardInterrupt:
            status = "STOPPED"
        except Exception as exc:
            status, error = "FAILED", type(exc).__name__
        final_stats = feed.stats()
    if status == "COMPLETED" and counts["signal_evaluations"] == 0:
        status = "NO_PAIRED_DATA"
    result = {
        "status": status,
        "symbol": symbol,
        "book_channels": channels,
        "mode": "order_flow_shadow",
        "error": error,
        "seconds": seconds,
        "sample_ms": sample_ms,
        "execution_enabled": False,
        "counts": counts,
        "stream": final_stats,
        "model": str(model_path),
    }
    write_json(output / "status.json", result)
    print(json.dumps(result), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    r = sub.add_parser("replay")
    r.add_argument("--symbol", required=True)
    r.add_argument("--archive", type=Path, required=True)
    r.add_argument("--output", type=Path, required=True)
    live_parser = sub.add_parser("live")
    live_parser.add_argument("--symbol", required=True)
    live_parser.add_argument("--model", type=Path, required=True)
    live_parser.add_argument("--output", type=Path, required=True)
    live_parser.add_argument("--seconds", type=float, default=60)
    args = parser.parse_args()
    if args.command == "replay":
        replay(args.archive, args.output, symbol=args.symbol)
    else:
        if not math.isfinite(args.seconds) or not 0 < args.seconds <= 1800:
            parser.error("seconds must be in (0,1800]")
        live(args.model, args.output, args.seconds, symbol=args.symbol)


if __name__ == "__main__":
    main()
