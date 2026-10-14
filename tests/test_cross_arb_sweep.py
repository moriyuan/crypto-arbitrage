"""Parameter selection is frozen before the holdout is touched."""
from dataclasses import asdict
from decimal import Decimal as D

import pytest

from btc_cta.cross_arb.entry_filters import EntryFilterConfig
from btc_cta.cross_arb.sweep import build_grid, choose_candidate, split_period


def row(identifier, train="1", validation="1", count=3, unresolved=False):
    return {"candidate_id": identifier, "role": "candidate", "config": asdict(EntryFilterConfig()),
            "train": {"net_before_funding": train, "closed_pairs": count, "unresolved": unresolved},
            "validation": {"net_before_funding": validation, "closed_pairs": count,
                           "unresolved": unresolved}}


def test_grid_default_has_twelve_configs_and_unfiltered_baseline():
    grid = build_grid([60], [D(0), D(5), D(10), D(20)], [D(250), D(500), D(1000)], [D(10)], 1000)
    assert len(grid) == 13
    assert len({tuple(asdict(config).values()) for _, _, config in grid}) == 13
    assert grid[-1][1] == "baseline"
    assert grid[-1][2].min_volatility_bps == grid[-1][2].min_depth_usdt == 0


def test_grid_rejects_duplicate_or_unbounded_search():
    with pytest.raises(ValueError):
        build_grid([60, 60], [D(5)], [D(500)], [D(10)], 1000)
    with pytest.raises(ValueError):
        build_grid([60], [D(i) for i in range(65)], [D(500)], [D(10)], 1000)


def test_choose_uses_validation_only_after_training_qualification():
    a, b = row("a", train="100", validation="1"), row("b", train="1", validation="2")
    a["holdout"] = {"net_before_funding": "999999"}
    b["holdout"] = {"net_before_funding": "-999999"}
    selected = choose_candidate([a, b], min_trades=3)
    assert selected["selected_candidate_id"] == "b"
    a["holdout"], b["holdout"] = b["holdout"], a["holdout"]
    assert choose_candidate([a, b], min_trades=3) == selected


def test_zero_trades_losses_unclosed_or_ablation_are_not_optima():
    values = [row("zero", count=0), row("loss", validation="-1"),
              row("open", unresolved=True), row("train_loss", train="-1"), row("off")]
    values[-1]["config"]["min_volatility_bps"] = D(0)
    result = choose_candidate(values, min_trades=3)
    assert result["status"] == "NO_QUALIFYING_PARAMETERS"
    assert result["selected_candidate_id"] is None
    assert result["holdout_role"] == "predeclared_default_diagnostic"


def test_split_is_contiguous_time_based_and_rejects_too_short_data():
    periods = split_period(1000, 1800999, minimum_segment_ms=121000)
    assert periods == {"train": (1000,1081000), "validation": (1081000,1441000),
                       "holdout": (1441000,1801000)}
    with pytest.raises(ValueError):
        split_period(1000, 1100, minimum_segment_ms=121000)


@pytest.mark.parametrize("bad_hash", [False, True])
@pytest.mark.parametrize("history_filtered", [False, True])
def test_sweep_real_paper_ledgers_and_funding_binding(
    tmp_path, monkeypatch, bad_hash, history_filtered,
):
    import hashlib
    import json

    from btc_cta.cross_arb.history_gate import HistoryGate, RollingHistoryConfig
    from btc_cta.cross_arb.market import Book
    from btc_cta.cross_arb.replay import ReplayData, ReplayPoint
    from btc_cta.cross_arb.sweep import run_search

    points = [ReplayPoint(t, {
        v: Book(v, ((D("99.9"), D(100)),), ((D(100), D(100)),), t, t, 0)
        for v in ("bybit", "bitget")
    }) for t in range(1000, 1001001, 1000)]
    audit = {"first_ms": 1000, "last_ms": 1001000, "symbol": "SKLUSDT",
             "archive_sha256": "a" * 64, "verified": True}
    data = ReplayData(points, audit)
    monkeypatch.setattr("btc_cta.cross_arb.replay.load_archive", lambda *a: data)
    rules_path, funding_path = tmp_path / "rules.json", tmp_path / "funding.json"
    rules_path.write_text(json.dumps({
        v: {"qty_step": "1", "min_qty": "1", "min_notional": "5", "price_tick": "0.1"}
        for v in ("bybit", "bitget")
    }))
    proof = {"symbol": "SKLUSDT", "raw": {"sha256": "b" * 64 if bad_hash else "a" * 64},
             "funding": {"assessment": "VERIFIED_NO_SETTLEMENT",
                         "checked_interval_received_ms": [1000, 1001000], "venues": {
                             v: {"verified_no_settlement": True, "coverage_brackets_interval": True,
                                 "settlements_in_received_interval_ms": [],
                                 "prior_settlement_ms": 1, "next_settlement_ms": 2000000}
                             for v in ("bybit", "bitget")}}}
    funding_path.write_text(json.dumps(proof))
    grid = build_grid([60], [D(5)], [D(500)], [D(10)], 1000,
                      history_filtered=history_filtered)
    gate = HistoryGate(RollingHistoryConfig(), {}) if history_filtered else None
    output = tmp_path / "search"
    if bad_hash:
        with pytest.raises(ValueError, match="exact archive"):
            run_search(tmp_path, rules_path, funding_path, output, grid,
                history_gate=gate, allow_legacy_unfiltered=not history_filtered)
        assert not (output / "candidates").exists()
        assert json.loads((output / "status.json").read_text())["status"] == "FAILED"
        return
    final = run_search(tmp_path, rules_path, funding_path, output, grid,
        history_gate=gate, allow_legacy_unfiltered=not history_filtered)
    assert final["status"] == "COMPLETED"
    assert final["selection"]["status"] == "NO_QUALIFYING_PARAMETERS"
    assert set(final["holdout"]) == {"fixed_default", "baseline"}
    assert all(x["orders"] == 0 and x["cash"] == {"bybit": "1000", "bitget": "1000"}
               for x in final["holdout"].values())
    assert len(list(output.rglob("ledger.sqlite"))) == 6
    assert hashlib.sha256((output / "selection.json").read_bytes()).hexdigest() == (
        final["selection_sha256_before_holdout"]
    )
    assert json.loads((output / "status.json").read_text())["completed_segments"] == 6
    if history_filtered:
        # Includes every candidate phase, the diagnostic holdout and both baselines.
        for path in output.rglob("segment.json"):
            segment = json.loads(path.read_text())
            assert segment["legacy_unfiltered"] is False
            assert segment["history_gate"]["config"]["top_n"] == 10
            assert segment["depth_sizing"]["participation"] == "0.25"
        assert all(item["history_filter_reasons"] == {
            "history_symbol_unavailable": item["signals"]
        } for item in final["holdout"].values())
    with pytest.raises(FileExistsError):
        run_search(tmp_path, rules_path, funding_path, output, grid,
                   history_gate=gate, allow_legacy_unfiltered=not history_filtered)
