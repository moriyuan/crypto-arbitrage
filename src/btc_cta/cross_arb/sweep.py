"""Offline chronological parameter search over a validated public L2 archive."""
from __future__ import annotations

import argparse
import csv
import hashlib
import itertools
import json
import os
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .depth_sizing import DepthSizingConfig
from .engine import Policy
from .entry_filters import EntryFilterConfig
from .history_gate import HistoryGate, RollingHistoryConfig, load_public_history
from .live import write_json
from .market import Rules

D = Decimal


def build_grid(
    windows: Sequence[int], volatilities: Sequence[Decimal], depths: Sequence[Decimal],
    bands: Sequence[Decimal], sample_ms: int, *, history_filtered: bool = False,
) -> list[tuple[str, str, EntryFilterConfig]]:
    axes: tuple[Sequence[Any], ...] = (windows, volatilities, depths, bands)
    if any(not values or len(set(values)) != len(values) for values in axes):
        raise ValueError("search axes must be nonempty and contain no duplicates")
    if len(windows) * len(volatilities) * len(depths) * len(bands) > 64:
        raise ValueError("at most 64 grid combinations are permitted")
    result = []
    for i, (window, volatility, depth, band) in enumerate(itertools.product(*axes)):
        config = EntryFilterConfig(window, sample_ms, volatility, band, depth)
        role = "candidate" if history_filtered or (volatility > 0 and depth > 0) else "ablation"
        result.append((f"c{i:03d}", role, config))
    baseline = EntryFilterConfig(min_volatility_bps=D(0), min_depth_usdt=D(0))
    result = [(i, role, cfg) for i, role, cfg in result if history_filtered or cfg != baseline]
    result.append(("baseline", "baseline", baseline))
    return result


def split_period(
    first_ms: int, last_ms: int, minimum_segment_ms: int
) -> dict[str, tuple[int, int]]:
    end = last_ms + 1
    span = end - first_ms
    train_end, validation_end = first_ms + span * 3 // 5, first_ms + span * 4 // 5
    periods = {"train": (first_ms, train_end), "validation": (train_end, validation_end),
               "holdout": (validation_end, end)}
    if minimum_segment_ms <= 0 or any(b - a <= minimum_segment_ms for a, b in periods.values()):
        raise ValueError("archive is too short for warmup, entry window, and reserved exit time")
    return periods


def choose_candidate(
    results: list[dict[str, Any]], min_trades: int = 3, *, history_filtered: bool = False,
) -> dict[str, Any]:
    if type(min_trades) is not int or min_trades <= 0:
        raise ValueError("minimum trade count must be positive")
    eligible = [r for r in results if r["role"] == "candidate"
                and (history_filtered or (D(r["config"]["min_volatility_bps"]) > 0
                                          and D(r["config"]["min_depth_usdt"]) > 0))
                and all(not r[phase]["unresolved"]
                        and r[phase]["closed_pairs"] >= min_trades
                        and D(r[phase]["net_before_funding"]) > 0
                        for phase in ("train", "validation"))]
    ranked = sorted(eligible, key=lambda r: (-D(r["validation"]["net_before_funding"]),
                                            r["candidate_id"]))
    return {
        "status": "SELECTED" if ranked else "NO_QUALIFYING_PARAMETERS",
        "selected_candidate_id": ranked[0]["candidate_id"] if ranked else None,
        "holdout_role": "selected_on_validation" if ranked else "predeclared_default_diagnostic",
        "ranking": [r["candidate_id"] for r in ranked],
        "minimum_closed_pairs_per_train_and_validation": min_trades,
        "criterion": "Both train and validation positive with enough closed pairs; rank by "
                     "validation net PnL. Ablations and baseline are never selected. "
                     "Holdout is not consulted; ties use candidate ID.",
    }


def _metrics(path: Path, summary: dict[str, Any]) -> dict[str, Any]:
    with (path / "cycles.csv").open(newline="") as f:
        cycles = list(csv.DictReader(f))
    reasons: Counter[str] = Counter()
    history_reasons: Counter[str] = Counter()
    with (path / "signals.csv").open(newline="") as f:
        for row in csv.DictReader(f):
            reasons[row.get("entry_filter_reason", "")] += 1
            history_reasons[row.get("history_filter_reason", "history_filter_disabled")] += 1
    return {
        "segment_status": summary["segment_status"],
        "net_before_funding": summary["closed_pair_net_before_funding"],
        "cash_change_before_funding": summary["cash_change_before_funding"],
        "closed_pairs": summary["completed_cycles"], "orders": summary["orders"],
        "signals": summary["signal_evaluations"], "eligible_signals": summary["eligible_signals"],
        "unresolved": summary.get("cycle") is not None,
        "funding_status": "UNRESOLVED" if summary.get("cycle") else "VERIFIED_NO_SETTLEMENT",
        "funding_usdt": None if summary.get("cycle") else "0",
        "cash": summary["cash"],
        "gross_pnl": str(sum((D(c["gross_pnl"]) for c in cycles), D(0))),
        "total_fees": str(sum((D(c["total_fees"]) for c in cycles), D(0))),
        "wins": sum(D(c["net_before_funding"]) > 0 for c in cycles),
        "max_holding_ms": max((int(c["holding_ms"]) for c in cycles), default=0),
        "entry_filter_reasons": dict(reasons), "history_filter_reasons": dict(history_reasons),
        "output": str(path),
    }


def _verify_funding(proof: dict[str, Any], audit: dict[str, Any]) -> None:
    funding = proof.get("funding", {})
    coverage = funding.get("checked_interval_received_ms", [])
    if (proof.get("symbol") != audit["symbol"]
            or proof.get("raw", {}).get("sha256") != audit["archive_sha256"]
            or funding.get("assessment") != "VERIFIED_NO_SETTLEMENT"
            or len(coverage) != 2
            or coverage[0] > audit["first_ms"] or coverage[1] < audit["last_ms"]):
        raise ValueError("funding evidence does not verify this exact archive and interval")
    for venue in ("bybit", "bitget"):
        item = funding.get("venues", {}).get(venue, {})
        if (item.get("verified_no_settlement") is not True
                or item.get("coverage_brackets_interval") is not True
                or item.get("settlements_in_received_interval_ms") != []
                or not item.get("prior_settlement_ms", audit["first_ms"]) < audit["first_ms"]
                or not item.get("next_settlement_ms", audit["last_ms"]) > audit["last_ms"]):
            raise ValueError("missing bracketing funding evidence for " + venue)


def run_search(
    run_dir: Path, rules_path: Path, funding_path: Path, output: Path,
    grid: list[tuple[str, str, EntryFilterConfig]], *, symbol: str = "SKLUSDT",
    fee_bps: Decimal = Decimal("3"), min_trades: int = 3,
    history_gate: HistoryGate | None = None, depth_sizing: DepthSizingConfig | None = None,
    allow_legacy_unfiltered: bool = False,
) -> dict[str, Any]:
    from .replay import load_archive, run_segment

    if history_gate is None and not allow_legacy_unfiltered:
        raise ValueError("historical ranking gate is required for parameter search")
    if history_gate is not None and not isinstance(history_gate, HistoryGate):
        raise ValueError("invalid historical ranking gate")
    if history_gate is not None and depth_sizing is None:
        depth_sizing = DepthSizingConfig()
    if depth_sizing is not None and not isinstance(depth_sizing, DepthSizingConfig):
        raise ValueError("invalid depth sizing configuration")
    history_filtered = history_gate is not None
    diagnostic = (EntryFilterConfig(min_volatility_bps=D(0), min_depth_usdt=D(0))
                  if history_filtered else EntryFilterConfig())
    if not fee_bps.is_finite() or not 0 <= fee_bps <= 100 or not grid:
        raise ValueError("invalid fee or empty parameter grid")
    choose_candidate([], min_trades, history_filtered=history_filtered)
    policy = Policy(max_cycles=10000, bybit_fee_bps=fee_bps, bitget_fee_bps=fee_bps)
    rules_json = json.loads(rules_path.read_text())
    if set(rules_json) != {"bybit", "bitget"}:
        raise ValueError("rules must cover exactly both venues")
    rules = {v: Rules(**{k: D(x) for k, x in value.items()}) for v, value in rules_json.items()}
    proof = json.loads(funding_path.read_text())
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    status: dict[str, Any] = {"status": "VALIDATING", "pid": os.getpid(), "symbol": symbol,
                              "execution_enabled": False, "completed_segments": 0}

    def progress(change: dict[str, Any]) -> None:
        status.update(change, heartbeat_utc=datetime.now(UTC).isoformat())
        write_json(output / "status.json", status)

    progress({})
    try:
        data = load_archive(run_dir, symbol)
        _verify_funding(proof, data.audit)
        longest = max(60, *(cfg.volatility_window_s for _, _, cfg in grid))
        periods = split_period(data.audit["first_ms"], data.audit["last_ms"],
                               int((longest + policy.max_holding_s + 2) * 1000))
        write_json(output / "input-audit.json", data.audit)
        write_json(output / "funding-evidence.json", proof)
        source_hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                         for p in Path(__file__).parent.glob("*.py")}
        metadata = {
            "symbol": symbol, "mode": "offline_parameter_search", "execution_enabled": False,
            "model_used": False, "archive": str(run_dir / "public-ws.jsonl.gz"),
            "archive_sha256": data.audit["archive_sha256"], "periods": periods,
            "policy": asdict(policy), "initial_balance_per_venue": "1000",
            "candidates": [{"candidate_id": i, "role": role, "config": asdict(cfg)}
                           for i, role, cfg in grid],
            "selection_rule": choose_candidate([], min_trades, history_filtered=history_filtered),
            "default_diagnostic_config": asdict(diagnostic),
            "history_gate": history_gate.metadata() if history_gate else None,
            "depth_sizing": asdict(depth_sizing) if depth_sizing else None,
            "legacy_unfiltered": history_gate is None,
            "funding": {"status": "VERIFIED_NO_SETTLEMENT", "funding_usdt": "0",
                        "evidence_used_for_accounting_only": True},
            "rules": rules_json, "rules_source": str(rules_path),
            "rules_caveat": "Same-day public snapshot taken after this recording; earlier "
                            "intra-day rule changes have not been independently ruled out.",
            "execution_assumption": (
                "Visible band depth times participation limits pair quantity; equal-size paired "
                "exit slices may remain unresolved. Each permitted slice assumes simultaneous "
                "full VWAP fill; no latency, asymmetric leg failure, or hidden liquidity model."
                if depth_sizing else "Legacy simultaneous full VWAP, no partial fills or latency."
            ),
            "source_sha256": source_hashes,
        }
        write_json(output / "run_metadata.json", metadata)
        results: list[dict[str, Any]] = []
        for candidate_id, role, config in grid:
            result: dict[str, Any] = {"candidate_id": candidate_id, "role": role,
                                     "config": asdict(config)}
            for phase in ("train", "validation"):
                progress({"status": "SEARCHING", "candidate_id": candidate_id, "phase": phase})
                path = output / "candidates" / candidate_id / phase
                start, end = periods[phase]
                summary = run_segment(
                    data, path, rules, policy, config, start, end, progress=progress,
                    history_gate=history_gate, depth_sizing=depth_sizing,
                    allow_legacy_unfiltered=allow_legacy_unfiltered,
                )
                result[phase] = _metrics(path, summary)
                progress({"completed_segments": status["completed_segments"] + 1})
            results.append(result)
            write_json(output / "search-results.json", {"results": results})
        selection = choose_candidate(results, min_trades, history_filtered=history_filtered)
        selected_id = selection["selected_candidate_id"]
        selected_config = next((cfg for i, _, cfg in grid if i == selected_id), diagnostic)
        selection.update(holdout_config=asdict(selected_config),
                         frozen_before_holdout_utc=datetime.now(UTC).isoformat())
        # This file is written once, before holdout simulation, and is never revised from its PnL.
        write_json(output / "selection.json", selection)
        frozen_hash = hashlib.sha256((output / "selection.json").read_bytes()).hexdigest()
        holdout: dict[str, Any] = {}
        for label, config in (("selected" if selected_id else "fixed_default", selected_config),
                              ("baseline", EntryFilterConfig(min_volatility_bps=D(0),
                                                               min_depth_usdt=D(0)))):
            progress({"status": "REPLAYING_HOLDOUT", "candidate_id": label, "phase": "holdout"})
            path = output / "holdout" / label
            summary = run_segment(data, path, rules, policy, config, *periods["holdout"],
                                  progress=progress, history_gate=history_gate,
                                  depth_sizing=depth_sizing,
                                  allow_legacy_unfiltered=allow_legacy_unfiltered)
            holdout[label] = _metrics(path, summary)
            progress({"completed_segments": status["completed_segments"] + 1})
        csv_rows = []
        for result in results:
            for phase in ("train", "validation"):
                metrics = result[phase]
                csv_rows.append({"candidate_id": result["candidate_id"], "role": result["role"],
                                 "phase": phase, **result["config"],
                                 **{k: v for k, v in metrics.items()
                                    if k not in {"cash", "entry_filter_reasons",
                                                 "history_filter_reasons"}}})
        with (output / "search-results.csv").open("x", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(csv_rows[0]))
            writer.writeheader()
            writer.writerows(csv_rows)
        unresolved = any(r[phase]["unresolved"] for r in results
                         for phase in ("train", "validation")) or any(
                             h["unresolved"] for h in holdout.values())
        final_status = "UNRESOLVED" if unresolved else "COMPLETED"
        final = {"status": final_status, "selection": selection, "holdout": holdout,
                 "selection_sha256_before_holdout": frozen_hash,
                 "funding": metadata["funding"], "candidate_count": len(grid),
                 "periods": periods, "archive_sha256": data.audit["archive_sha256"]}
        assert hashlib.sha256((output / "selection.json").read_bytes()).hexdigest() == frozen_hash
        write_json(output / "summary.json", final)
        lines = [f"# {symbol} 公开L2离线回放与搜参", "",
                 f"- 参数组数：{len(grid)}；时间按60%/20%/20%切分。",
                 "- 历史排名和盘口仓位约束应用到每个配置，也应用到baseline。"
                 if history_filtered else "- 此次显式使用旧版未加历史门槛的复现接口。",
                 f"- 每段独立本金：两所各1000 USDT；每次成交手续费{fee_bps}bp。",
                 "- 各段提前60秒停止新增并请求退出，边界后盘口不用于成交；未平仓保留。",
                 "- 资金费：该档案区间经历史结算记录核实为0，仅用于事后损益核算。",
                 f"- 参数选择状态：{selection['status']}；选中ID：{selected_id}。",
                 f"- 训练和验证各至少{min_trades}组且各自净损益为正才能入选；按验证净损益排序。",
                 "- 没有合格参数时，留出只评估预先指定默认配置，不称为最优参数。", "",
                 "| 留出配置 | 组合 | 订单 | 手续费后损益(U) | 未平仓 |",
                 "|---|---:|---:|---:|---|"]
        for label, item in holdout.items():
            lines.append(f"| {label} | {item['closed_pairs']} | {item['orders']} | "
                         f"{item['net_before_funding']} | {item['unresolved']} |")
        lines += ["", "完整搜索结果见search-results.csv/json；每组每段保留ledger与四类CSV。",
                  "有限样本不能证明实盘有效；每个许可数量假设双腿同步VWAP成交，未建模延迟或单腿失败。",
                  "分批退出可能产生超过4条订单，验收应按各腿累计开平数量、手续费和现金核对；",
                  "合约规则采用稍后同日快照，无法完全排除此前规则变化。参数与选择文件在留出前冻结。"]
        (output / "report.md").write_text("\n".join(lines) + "\n")
        progress({"status": final_status, "selection_status": selection["status"]})
        return final
    except Exception as exc:
        progress({"status": "FAILED", "error": type(exc).__name__, "detail": str(exc)[:500]})
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--rules", type=Path, required=True)
    parser.add_argument("--funding-audit", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--symbol", default="SKLUSDT")
    parser.add_argument("--history-dir", type=Path, required=True,
                        help="raw hourly market history and universe metadata directory")
    parser.add_argument("--identity-manifest", type=Path, required=True,
                        help="verified project identities, never a historical rank whitelist")
    parser.add_argument("--history-days", type=int, default=20)
    parser.add_argument("--history-top-n", type=int, default=10)
    parser.add_argument("--history-min-volatility-bps", type=D, default=D(0))
    parser.add_argument("--history-min-turnover-usdt", type=D, default=D(0))
    parser.add_argument("--history-publication-delay-ms", type=int, default=60000)
    parser.add_argument("--execution-band-bps", type=D, default=D(10))
    parser.add_argument("--depth-participation", type=D, default=D("0.25"))
    parser.add_argument("--window-grid", default="60")
    parser.add_argument("--volatility-grid", default="0")
    parser.add_argument("--depth-grid", default="0")
    parser.add_argument("--band-grid", default="10")
    parser.add_argument("--sample-ms", type=int, default=1000)
    parser.add_argument("--fee-bps", type=D, default=D(3))
    parser.add_argument("--min-trades", type=int, default=3)
    args = parser.parse_args()
    grid = build_grid([int(x) for x in args.window_grid.split(",")],
                      [D(x) for x in args.volatility_grid.split(",")],
                      [D(x) for x in args.depth_grid.split(",")],
                      [D(x) for x in args.band_grid.split(",")], args.sample_ms,
                      history_filtered=True)
    history_gate = load_public_history(
        args.history_dir,
        RollingHistoryConfig(window_days=args.history_days,
                             min_volatility_bps=args.history_min_volatility_bps,
                             min_turnover_usdt=args.history_min_turnover_usdt,
                             top_n=args.history_top_n,
                             publication_delay_ms=args.history_publication_delay_ms),
        identity_path=args.identity_manifest,
    )
    sizing = DepthSizingConfig(band_bps=args.execution_band_bps,
                               participation=args.depth_participation)
    result = run_search(args.run_dir, args.rules, args.funding_audit, args.output, grid,
                        symbol=args.symbol, fee_bps=args.fee_bps, min_trades=args.min_trades,
                        history_gate=history_gate, depth_sizing=sizing)
    print(json.dumps({"output": str(args.output), "status": result["status"],
                      "selection": result["selection"]["status"]}), flush=True)


if __name__ == "__main__":
    main()
